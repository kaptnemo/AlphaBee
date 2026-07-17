"""Thesis-generation node."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.enhancer import ThesisEnhancer
from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _build_conflict_data, _finalize_step, _find_artifact, _make_id
from alphabee.orchestrator.services.company_context import build_company_context
from alphabee.orchestrator.state import OrchestratorState


async def run_thesis(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run ThesisEngine on signal results, with optional LLM enhancement."""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    query = run.context.get("query", "") if run else ""
    financial_facts = state.get("financial_facts")
    market_facts = state.get("market_facts")
    enhance = state.get("enhance", False)

    step = Step(
        id="run_thesis",
        kind="run_thesis",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    # Thesis 层消费的是“已结构化、已归因”的中间结果：
    # 信号提供方向性判断，事实摘要提供定性背景，异常/冲突提供反证和疑点。
    signal_av = _find_artifact(state.get("artifacts", []), "signal_analysis")
    signal_results: dict = signal_av.get("results", {}) if signal_av else {}

    fc_av = _find_artifact(state.get("artifacts", []), "fact_collection")
    fact_text: str = fc_av.get("raw_response", "") if fc_av else ""

    anomaly_av = _find_artifact(state.get("artifacts", []), "anomaly_report")
    anomaly_data: dict = {}
    if anomaly_av:
        anomaly_data = {
            "anomaly_count": anomaly_av.get("anomaly_count", 0),
            "pattern_count": anomaly_av.get("pattern_count", 0),
            "anomalies": [
                item for item in anomaly_av.get("anomalies", [])
                if item.get("level") != "none"
            ],
            "pattern_matches": anomaly_av.get("pattern_matches", []),
        }

    if not signal_results:
        # ThesisEngine 的输入核心是 signal_results。
        # 没有信号就意味着无法把事实压缩成投资维度判断，
        # 因而宁可跳过，也不制造主观结论。
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="missing_data",
                message="No signal results available — skipping ThesisEngine.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    try:
        period = "latest"
        if financial_facts is not None and financial_facts.snapshots:
            snap_period = financial_facts.snapshots[0].period
            if snap_period:
                period = snap_period

        # CompanyContext 把公司所处行业、规模、生命周期等“解释框架”补齐，
        # 避免 thesis 只看到孤立指标，却不知道这些指标在什么商业场景下成立。
        company_ctx = build_company_context(
            symbol=symbol,
            fact_text=fact_text,
            financial_facts=financial_facts,
            market_facts=market_facts,
        )

        thesis_engine = ThesisEngine()
        thesis = thesis_engine.run(
            symbol=symbol or "unknown",
            period=period,
            signal_results=signal_results,
            anomaly_report=anomaly_av,
            conflict_analysis=state.get("conflicts_result"),
            verification_results=state.get("verification_results"),
            company_context=company_ctx,
        )

        enhanced = None
        if enhance:
            try:
                # enhancer 是可选“表达增强层”：
                # 它不改变底层确定性信号，只尝试补充跨信号模式和上下文化说明，
                # 让最终论点更贴近真实投研表达。
                enhancer = ThesisEnhancer()
                enhanced = enhancer.enhance(
                    thesis=thesis,
                    signal_results=signal_results,
                    company_context=company_ctx,
                    user_intent=query,
                    fact_summary=fact_text[:2000] if fact_text else "",
                )
            except Exception:
                pass

        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="thesis_analysis",
                producer_step=step.id,
                value={
                    # artifact 同时保留 thesis 主体、增强结果、行业语境、异常/冲突摘要，
                    # 让 report 和 review 都能在一个对象里读取完整论点上下文。
                    "thesis": thesis.to_dict(),
                    "enhanced": enhanced.to_dict() if enhanced else None,
                    "industry_context": {
                        "industry": company_ctx.industry,
                        "sub_industry": company_ctx.sub_industry,
                        "market_cap_category": company_ctx.market_cap_category,
                        "lifecycle_stage": company_ctx.lifecycle_stage,
                        "business_model_summary": (
                            company_ctx.business_model_summary[:300]
                            if company_ctx.business_model_summary
                            else ""
                        ),
                    },
                    "anomaly_data": anomaly_data,
                    "conflict_data": _build_conflict_data(state),
                },
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"ThesisEngine failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
    }
