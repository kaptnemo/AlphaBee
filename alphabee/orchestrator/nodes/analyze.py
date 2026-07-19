"""Deterministic analysis node for derived facts, anomalies, and signals."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from alphabee.agents.derived_facts.engine import Engine as DerivedFactsEngine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SIGNAL_RULES, load_signal_rules
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.contracts import (
    AnomalyReportArtifact,
    DerivedFactsArtifact,
    SignalAnalysisArtifact,
)
from alphabee.orchestrator.services.gap_recorder import record_signal_data_gaps
from alphabee.orchestrator.services.payload_builders import default_anomaly_fact_values
from alphabee.orchestrator.state import OrchestratorState


async def run_analysis_engines(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Run deterministic analysis engines on the structured fact values."""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    fact_values: dict[str, float] = dict(state.get("fact_values") or {})
    financial_facts = state.get("financial_facts")

    step = Step(
        id="run_analysis_engines",
        kind="run_analysis_engines",
        inputs={"symbol": symbol, "fact_values_count": len(fact_values)},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    if not fact_values:
        # 没有 canonical numeric facts 时，后面的三类确定性引擎都无法成立：
        # 1) derived facts 无法算财务比率
        # 2) anomaly 无法做历史勾稽偏离
        # 3) signals 无法做规则命中
        # 因此这里直接短路，避免产出“看起来像分析、实则无根”的结果。
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message="No fact_values available — skipping all analysis engines.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    derived_facts_payload = DerivedFactsArtifact()
    try:
        # 第一层：把原始财务/市场字段转换成更贴近投资分析的话语单元，
        # 例如增长质量、盈利质量、现金流质量等中间指标。
        # 这些 derived facts 本身仍是确定性计算，后续信号和 thesis 都会复用。
        load_rules()
        df_engine = DerivedFactsEngine()
        all_rule_names = list(RULES.keys())
        derived_facts_payload = DerivedFactsArtifact(
            results=df_engine.run(all_rule_names, fact_values),
            rule_count=len(all_rule_names),
        )
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.DERIVED_FACTS,
                producer_step=step.id,
                value=derived_facts_payload.model_dump(mode="json"),
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"DerivedFacts engine failed: {exc}",
                related_step=step.id,
            )
        )

    anomaly_report_payload: AnomalyReportArtifact | None = None
    anomaly_fact_values: dict[str, float] = default_anomaly_fact_values()
    if financial_facts is not None and len(financial_facts.snapshots) >= 2:
        try:
            from alphabee.agents.anomaly.engine import AnomalyEngine

            extra_vals: dict[str, float] = {}
            try:
                # 异常检测优先使用财务快照；员工数等经营规模字段若拿得到，
                # 可以辅助判断异常是否来自扩张/收缩阶段，而不是纯会计噪声。
                profile = get_company_profile(symbol)
                company_data = profile.get("company", {}) if profile else {}
                employees_raw = company_data.get("employees", {})
                if isinstance(employees_raw, dict):
                    employees_val = employees_raw.get(0)
                    if employees_val is not None:
                        extra_vals["employees"] = float(employees_val)
            except Exception:
                pass

            anomaly_engine = AnomalyEngine()
            anomaly_report = anomaly_engine.run(
                financial_facts,
                extra_values=extra_vals or None,
            )
            # 关键设计：异常结果会再投影回 fact_values。
            # 这样 signal rules 就能把“应收异常”“存货模式异常”当成标准事实处理，
            # 实现从原始报表 → 异常识别 → 风险信号的分层传导。
            anomaly_fact_values.update(anomaly_report.to_fact_values())
            anomaly_report_payload = AnomalyReportArtifact.model_validate(anomaly_report.to_dict())
            new_artifacts.append(
                Artifact(
                    id=_make_id("artifact"),
                    type=ArtifactType.ANOMALY_REPORT,
                    producer_step=step.id,
                    value=anomaly_report_payload.model_dump(mode="json"),
                )
            )
        except Exception as exc:
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="subagent_failure",
                    message=f"AnomalyEngine failed: {exc}",
                    related_step=step.id,
                )
            )

    fact_values.update(anomaly_fact_values)

    signal_analysis_payload = SignalAnalysisArtifact()
    try:
        # 第二层：SignalEngine 不重新理解报表，而是消费 fact_values /
        # derived facts / anomaly facts，统一输出可审计的风险或机会信号。
        load_signal_rules()
        signal_engine = SignalEngine()
        all_signal_names = list(SIGNAL_RULES.keys())
        signal_analysis_payload = SignalAnalysisArtifact(
            results=signal_engine.run(all_signal_names, fact_values),
            rule_count=len(all_signal_names),
        )
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.SIGNAL_ANALYSIS,
                producer_step=step.id,
                value=signal_analysis_payload.model_dump(mode="json"),
            )
        )
        # 对 blocked / missing_fact 信号单独落库，
        # 便于后续观察数据源缺口究竟阻塞了哪些业务规则。
        record_signal_data_gaps(signal_analysis_payload.results, fact_values, symbol)
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"SignalEngine failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
        "fact_values": fact_values,
    }
