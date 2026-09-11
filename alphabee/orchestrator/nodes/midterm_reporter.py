"""midterm_decision_reporter 节点 — 把 MIDTERM_DECISION 渲染成可读总结并完整落日志。

背景：中期决策层仍在 WIP，与整体报告之间存在 GAP，因此暂不把决策写进
``generate_report`` 的报告。本节点作为独立的「决策 reporter」：

1. 读取 ``MIDTERM_DECISION`` artifact（``CompanyStateArtifact``）；
2. 用确定性模板渲染完整的中文可读总结（不调 LLM、不截断、无失败点）；
3. 把完整总结写入日志（``logger.info``，落 ``logs/alpha_arena.log``）；
4. 产出 ``MIDTERM_DECISION_SUMMARY`` artifact（只进 artifacts / 载荷，不进报告）。

降级纪律：缺少 ``MIDTERM_DECISION``（上游决策节点失败）时 SKIPPED，不报 issue；
渲染异常时记 Issue 但不中断主链（报告照常）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.midterm.models import CompanyStateArtifact
from alphabee.orchestrator.contracts import (
    MidtermDecisionSummaryArtifact,
    find_artifact_model,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import get_logger
from alphabee.utils.pipeline import make_id

_logger = get_logger("orchestrator.midterm_reporter")

# 七因子展示顺序（label, FactorSnapshot 属性名）。
_FACTOR_LABELS: tuple[tuple[str, str], ...] = (
    ("F 基本面", "fundamental"),
    ("E 预期", "expectation"),
    ("T 趋势", "trend"),
    ("V 估值", "valuation"),
    ("C 拥挤", "crowding"),
    ("R 风险", "risk"),
    ("M 市场", "market"),
)

_VARIABLE_LABELS: tuple[tuple[str, str], ...] = (
    ("F", "f_fundamental_trend"),
    ("E", "e_revision"),
    ("T", "t_relative_strength"),
    ("V", "v_valuation_percentile"),
    ("C", "c_crowding"),
    ("R", "r_risk"),
)


def _fmt(value: Any, *, digits: int = 4) -> str:
    """确定性格式化：``None`` → ``—``，数字保留位数，bool → 是/否，dict 展开。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}g}"
    if isinstance(value, dict):
        if not value:
            return "—"
        return "  ".join(f"{k}={_fmt(v, digits=digits)}" for k, v in value.items())
    text = str(value).strip()
    return text or "—"


def _section(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(f"── {title} " + "─" * max(0, 56 - len(title)))


def _render_state(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "认知状态（软状态分布）")
    state = decision.state
    if state is None:
        lines.append("  （缺失：state=None）")
        return
    lines.append(f"  argmax: {state.argmax_state or '—'}    熵: {_fmt(state.entropy)}")
    distribution = "  ".join(f"{k}={_fmt(v)}" for k, v in sorted(state.distribution.items()))
    lines.append(f"  分布: {distribution or '—'}")
    if state.drift:
        drift = "  ".join(f"{k}={_fmt(v)}" for k, v in state.drift.items())
        lines.append(f"  漂移: {drift}")


def _render_hypothesis(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "核心假设与置信度")
    lines.append(f"  H: {decision.thesis or '—'}")
    lines.append(f"  后验 P(H|E): {_fmt(decision.thesis_confidence)}    先验 P(H): {_fmt(decision.prior_confidence)}")


def _render_expectation_gap(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "预期差")
    gap = decision.expectation_gap
    lines.append(
        f"  gap={_fmt(gap.gap)}    direction={gap.gap_direction or '—'}    stale_after={gap.stale_after or '—'}"
    )
    lines.append(f"  我们预测: {_fmt(gap.our_forecast)}")
    lines.append(f"  市场隐含: {_fmt(gap.implied_expectation)}")
    if gap.evidence:
        for item in gap.evidence:
            lines.append(f"    - {item}")
    else:
        lines.append("    证据: —")


def _render_variable_scores(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "变量方向分（[-1,1]，正=对多头有利）")
    scores = decision.variable_scores
    for label, attr in _VARIABLE_LABELS:
        lines.append(f"  {label}: {_fmt(getattr(scores, attr))}")
    if scores.m:
        lines.append(f"  M(regime 摘要): {_fmt(scores.m)}")


def _render_factor_snapshot(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "七因子快照")
    snapshot = decision.factor_snapshot
    if snapshot is None:
        lines.append("  （缺失：factor_snapshot=None）")
        return
    lines.append(
        f"  state={snapshot.state or '—'}  confidence={_fmt(snapshot.confidence)}  as_of={snapshot.as_of_date or '—'}"
    )
    for label, attr in _FACTOR_LABELS:
        factor = getattr(snapshot, attr, None)
        if factor is None:
            continue
        data = factor.model_dump(exclude_none=True)
        direction = data.pop("direction", "")
        score = data.pop("score", None)
        lines.append(f"  {label}: direction={direction or '—'}  score={_fmt(score)}")
        if data:
            lines.append(f"      {_fmt(data)}")
    if snapshot.missing_facts:
        lines.append(f"  缺失字段: {', '.join(snapshot.missing_facts)}")


def _render_expected_value(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "赔率 / 期望值")
    ev = decision.expected_value
    if ev is None:
        lines.append("  （缺失：expected_value=None）")
        return
    lines.append(
        f"  EV={_fmt(ev.ev)}    风险={_fmt(ev.risk)}    风险调整EV={_fmt(ev.risk_adjusted_ev)}"
        f"    概率来源={ev.probability_source or '—'}"
    )
    for scenario in ev.scenarios:
        lines.append(
            f"    {scenario.scenario or '—'}: P={_fmt(scenario.probability)}  R={_fmt(scenario.expected_return)}"
            f"  earnings={_fmt(scenario.earnings_contribution)}  valuation={_fmt(scenario.valuation_contribution)}"
            f"  maxDD={_fmt(scenario.max_drawdown)}"
        )
    if ev.note:
        lines.append(f"  说明: {ev.note}")


def _render_evidence(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, f"证据日志（{len(decision.evidence_log)} 条）")
    if not decision.evidence_log:
        lines.append("  （无证据：state_prior 保守版）")
        return
    for idx, event in enumerate(decision.evidence_log, start=1):
        refs = f"  refs={', '.join(event.source_refs)}" if event.source_refs else ""
        lines.append(
            f"  [{idx}] {event.date or '—'}  {event.kind or '—'}  {event.effect_on_thesis or '—'}"
            f"  Δ={_fmt(event.confidence_delta)}{refs}"
        )
        lines.append(f"      {event.description or '—'}")


def _render_watch_and_exit(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "待观察证据 / 退出条件")
    if decision.next_evidence_to_watch:
        lines.append("  待观察:")
        for item in decision.next_evidence_to_watch:
            lines.append(f"    - {item}")
    else:
        lines.append("  待观察: —")
    if decision.exit_conditions:
        lines.append("  退出条件:")
        for condition in decision.exit_conditions:
            mark = "已满足" if condition.met else "未满足"
            lines.append(f"    - [{mark}] {condition.kind or '—'}: {condition.condition or '—'}")
    else:
        lines.append("  退出条件: —")


def _render_position(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "仓位决策")
    position = decision.position
    if position is None:
        lines.append("  （缺失：position=None）")
        return
    lines.append(
        f"  band={position.position_band or '—'}    市场暴露={_fmt(position.portfolio_exposure)}"
        f"    个股权重={_fmt(position.stock_weight)}    实际权重={_fmt(position.actual_weight)}"
        f"    受限={_fmt(position.restricted)}"
    )
    if position.rationale:
        for reason in position.rationale:
            lines.append(f"    - {reason}")


def _render_meta(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "元信息")
    lines.append(
        f"  schema_version={decision.schema_version or '—'}    symbol={decision.symbol or '—'}"
        f"    as_of={decision.as_of_date or '—'}    stale_after={decision.stale_after or '—'}"
    )
    lines.append(f"  degraded={_fmt(decision.degraded)}    reason={decision.degraded_reason or '—'}")


def render_midterm_decision_summary(decision: CompanyStateArtifact) -> str:
    """把 ``CompanyStateArtifact`` 渲染成完整、可读、可审计的中文总结文本。"""
    lines: list[str] = [
        "=" * 64,
        f"中期决策总结  symbol={decision.symbol or '—'}  as_of={decision.as_of_date or '—'}",
        "=" * 64,
    ]
    _render_state(lines, decision)
    _render_hypothesis(lines, decision)
    _render_expectation_gap(lines, decision)
    _render_variable_scores(lines, decision)
    _render_factor_snapshot(lines, decision)
    _render_expected_value(lines, decision)
    _render_evidence(lines, decision)
    _render_watch_and_exit(lines, decision)
    _render_position(lines, decision)
    _render_meta(lines, decision)
    lines.append("")
    lines.append("=" * 64)
    return "\n".join(lines)


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _finalize_step(step: Step, issues: list[Issue], artifacts: list[Artifact]) -> Step:
    """与 collectors._finalize_step 同语义的本地实现（避免传递性 tushare import）。"""
    if issues and not artifacts:
        status = StepStatus.FAILED
    elif issues:
        status = StepStatus.PARTIAL
    else:
        status = StepStatus.SUCCEEDED
    return step.model_copy(update={"status": status, "outputs": [a.id for a in artifacts]})


async def report_midterm_decision(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """渲染 MIDTERM_DECISION 总结，完整落日志，并产出 summary artifact（不进报告）。"""
    del config
    artifacts = state.get("artifacts", [])
    step = Step(
        id="midterm_decision_reporter",
        kind="midterm_decision_reporter",
        inputs={"artifact_count": len(artifacts)},
        status=StepStatus.RUNNING,
    )

    decision = find_artifact_model(artifacts, ArtifactType.MIDTERM_DECISION, CompanyStateArtifact)
    if decision is None:
        # 上游决策节点失败/未产出：本节点静默跳过，不重复报 issue。
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    new_issues: list[Issue] = []
    new_artifacts: list[Artifact] = []
    try:
        text = render_midterm_decision_summary(decision)
        _logger.info(
            "midterm_decision_summary",
            symbol=decision.symbol,
            as_of_date=decision.as_of_date,
            degraded=decision.degraded,
            summary=text,
        )
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.MIDTERM_DECISION_SUMMARY,
                producer_step=step.id,
                value=MidtermDecisionSummaryArtifact(
                    symbol=decision.symbol,
                    as_of_date=decision.as_of_date,
                    text=text,
                    degraded=decision.degraded,
                ).model_dump(mode="json"),
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.LOW,
                category="midterm_decision_report_failed",
                message=f"midterm_decision_reporter failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "artifacts": new_artifacts,
        "issues": new_issues,
    }
