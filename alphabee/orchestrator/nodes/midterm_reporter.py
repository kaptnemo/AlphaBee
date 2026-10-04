"""midterm_decision_reporter 节点 — 把 MIDTERM_DECISION 渲染成可读总结并完整落日志。

背景：中期决策层仍在 WIP，与整体报告之间存在 GAP，因此暂不把决策写进
``generate_report`` 的报告。本节点作为独立的「决策 reporter」：

1. 读取 ``MIDTERM_DECISION`` artifact（``CompanyStateArtifact``）；
2. 用确定性模板渲染完整的中文可读总结（不调 LLM、不截断、无失败点）；
3. 把完整总结写入日志（``logger.info``，落 ``logs/alpha_arena.log``）；
4. 产出 ``MIDTERM_DECISION_SUMMARY`` artifact（只进 artifacts / 载荷，不进报告）。

呈现口径（P2-6，用户外部数据交叉核对挖出的三项误读）：

1. **两个时点必须分开标注**。头部显式区分「财报口径」（财务数据报告期，如
   ``20260630``）与「估值/行情口径」（行情截止日，如 ``20260930`` ＋ 收盘价 /
   PE_TTM / PB）。二者不是同一天是常态，只写一个日期会让读者把 PE_TTM / PB /
   收盘价误读为财报期末估值。缺失时显式写「（未提供）」——**绝不用决策日或采集日
   顶替行情截止日**（那正是本项要消除的口径混同）。
2. **regime 进入正文并给出仓位成因**。「市场环境（regime，宏观约束）」章节呈现
   市场阶段 / 沪深300 / 建议暴露带，并显式说明仓位档位（如「观察」）是 market_regime
   层面的宏观约束，而非个股基本面判断的降级。
3. **后验与状态确信度分开呈现**。``thesis_confidence``（如 0.95）是**假设 H 的贝叶斯
   后验 P(H|E)**；状态确信度是 argmax 状态的**概率质量**（如 S4=0.377）。两者各自
   独立成行并互相显式区分，不得并列成「状态 S4 置信度 0.95」。

降级纪律：缺少 ``MIDTERM_DECISION``（上游决策节点失败）时 SKIPPED，不报 issue；
渲染异常时记 Issue 但不中断主链（报告照常）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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

# M 因子（market_regime）摘要字段：优先取 factor_snapshot.market，退回 variable_scores.m。
_MARKET_FIELDS: tuple[str, ...] = (
    "regime",
    "market_score",
    "hs300_close",
    "hs300_pe_ttm",
    "hs300_pb",
    "position_low",
    "position_high",
)


@dataclass(frozen=True)
class ReportTimePoints:
    """报告头部要分开标注的两个数据口径时点（P2-6）。

    - ``financial_period``：**财报口径**（财务数据报告期，``YYYYMMDD``）；来源
      ``state["financial_facts"].snapshots[0].period``（上游 ``FinancialSnapshot.period``）。
    - ``valuation_as_of``：**估值/行情口径**（行情数据截止日，``YYYYMMDD``）；来源
      ``state["market_facts"].trade_date``，随附 ``close_price`` / ``pe_ttm`` / ``pb_ratio``。

    两者缺失时保持空串（渲染为「（未提供）」）。**不做任何兜底替换**：行情截止日缺失时
    不用决策日 / 采集日顶替，否则等于把「估值时点」重新混同回另一个日期——正是 P2-6 要
    消除的误读来源。
    """

    financial_period: str = ""
    valuation_as_of: str = ""
    valuation_close: float | None = None
    valuation_pe_ttm: float | None = None
    valuation_pb: float | None = None


def _attr(obj: Any, name: str) -> Any:
    """容忍 pydantic 模型与已 ``model_dump`` 的 dict 两种形态取值（只读）。"""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_time_points(state: Mapping[str, Any]) -> ReportTimePoints:
    """从 ``OrchestratorState`` 提取两个口径的时点（纯读取、无网络、无 LLM、失败即空）。

    只读 ``financial_facts`` / ``market_facts`` 两个**已采集**状态字段，不做任何新采集、
    不做日期推算：财报口径取最新财务快照的 ``period``，行情口径取行情事实的 ``trade_date``。
    """
    financial_facts = state.get("financial_facts")
    snapshots = _attr(financial_facts, "snapshots") or []
    financial_period = str(_attr(snapshots[0], "period") or "") if len(snapshots) else ""

    market_facts = state.get("market_facts")
    return ReportTimePoints(
        financial_period=financial_period,
        valuation_as_of=str(_attr(market_facts, "trade_date") or ""),
        valuation_close=_attr(market_facts, "close_price"),
        valuation_pe_ttm=_attr(market_facts, "pe_ttm"),
        valuation_pb=_attr(market_facts, "pb_ratio"),
    )


def _fmt_ratio(value: Any) -> str:
    """比率/点位格式化：去掉无意义尾零（``0.0 → 0``、``0.3 → 0.3``、``4358.0 → 4358``）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    return text or "—"


def _market_view(decision: CompanyStateArtifact) -> dict[str, Any]:
    """M 因子摘要视图（P2-6）：优先 ``factor_snapshot.market``，退回 ``variable_scores.m``。

    两条通道都缺失时返回 ``{}``（调用方显式呈现「未采集」，不静默省略该章节）。
    """
    snapshot = decision.factor_snapshot
    market = snapshot.market if snapshot is not None else None
    if market is not None:
        view = {field: getattr(market, field, None) for field in _MARKET_FIELDS}
        if any(value not in (None, "") for value in view.values()):
            return view
    raw = decision.variable_scores.m
    if isinstance(raw, Mapping):
        view = {field: raw.get(field) for field in _MARKET_FIELDS}
        if any(value not in (None, "") for value in view.values()):
            return view
    return {}


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


def _render_header(lines: list[str], decision: CompanyStateArtifact, time_points: ReportTimePoints) -> None:
    """报告头部（P2-6）：显式区分「财报口径」与「估值/行情口径」两个时点。

    只写一个日期（此前只有 ``as_of``）会让读者把 PE_TTM / PB / 收盘价当成财报期末估值；
    这里把两个口径各自的截止日分行标注，缺失则写「（未提供）」。
    """
    lines.append("=" * 64)
    lines.append(f"中期决策总结  symbol={decision.symbol or '—'}")
    lines.append(f"  财报口径（财务数据报告期）: {time_points.financial_period or '（未提供）'}")
    valuation_line = f"  估值/行情口径（行情截止日）: {time_points.valuation_as_of or '（未提供）'}"
    quotes = [
        f"{label} {_fmt(value, digits=5)}"
        for label, value in (
            ("收盘价", time_points.valuation_close),
            ("PE_TTM", time_points.valuation_pe_ttm),
            ("PB", time_points.valuation_pb),
        )
        if value is not None
    ]
    if quotes:
        valuation_line += "    " + "  ".join(quotes)
    lines.append(valuation_line)
    lines.append(
        "  口径说明: 两个时点不同属常态——收盘价 / PE_TTM / PB 属估值/行情口径，"
        "不得按财报期末口径解读，也不得与财报期共用同一日期。"
    )
    lines.append("=" * 64)


def _state_confidence_phrase(decision: CompanyStateArtifact) -> str:
    """状态确信度短语（P2-6）：argmax 状态的概率质量，供后验口径说明交叉引用。"""
    state = decision.state
    if state is None:
        return "state 缺失，无状态确信度可比"
    mass = state.distribution.get(state.argmax_state) if state.argmax_state else None
    return f"{state.argmax_state or '—'} 概率质量 {_fmt(mass)}"


def _render_state(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "认知状态（软状态分布）")
    state = decision.state
    if state is None:
        lines.append("  （缺失：state=None）")
        return
    lines.append(f"  argmax: {state.argmax_state or '—'}    熵: {_fmt(state.entropy)}")
    distribution = "  ".join(f"{k}={_fmt(v)}" for k, v in sorted(state.distribution.items()))
    lines.append(f"  分布: {distribution or '—'}")
    # P2-6：状态确信度 = argmax 状态的概率质量，与假设 H 的后验是两回事，必须独立成行，
    # 否则读者会把 thesis_confidence（如 0.95）当成「状态 S4 的确信度」。
    mass = state.distribution.get(state.argmax_state) if state.argmax_state else None
    lines.append(f"  状态确信度: {state.argmax_state or '—'}={_fmt(mass)}（argmax 状态的概率质量，不是假设 H 的后验）")
    if state.drift:
        drift = "  ".join(f"{k}={_fmt(v)}" for k, v in state.drift.items())
        lines.append(f"  漂移: {drift}")


def _render_hypothesis(lines: list[str], decision: CompanyStateArtifact) -> None:
    _section(lines, "核心假设与置信度")
    lines.append(f"  H: {decision.thesis or '—'}")
    lines.append(f"  后验 P(H|E): {_fmt(decision.thesis_confidence)}    先验 P(H): {_fmt(decision.prior_confidence)}")
    # P2-6：后验属于「假设 H」，不是状态确信度；显式交叉引用「认知状态」节，消除并列误导。
    lines.append(
        f"  口径: {_fmt(decision.thesis_confidence)} 是假设 H 的贝叶斯后验 P(H|E)，不是状态确信度；"
        f"状态确信度见「认知状态」节（{_state_confidence_phrase(decision)}），两者不可互换。"
    )


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


def _render_market_regime(lines: list[str], decision: CompanyStateArtifact) -> None:
    """市场环境（regime）章节（P2-6）：宏观约束进入正文，并给出仓位档位的成因。

    此前 regime 只以 ``M(regime 摘要)`` 的原始 dict 出现在变量分里，正文从未说明
    「仓位=观察」这类档位是由 market_regime 的建议暴露带约束出来的，读者会把它误读为
    个股基本面判断的降级。本节把市场阶段 / 沪深300 / 暴露带与成因关系显式写出。
    """
    _section(lines, "市场环境（regime，宏观约束）")
    market = _market_view(decision)
    if not market:
        lines.append("  （缺失：market_regime 未采集，无法呈现宏观约束与仓位成因）")
        return
    band = f"{_fmt_ratio(market.get('position_low'))}–{_fmt_ratio(market.get('position_high'))}"
    lines.append(
        f"  市场阶段: {_fmt(market.get('regime'))}    市场得分: {_fmt(market.get('market_score'))}"
        f"    建议暴露带: {band}"
    )
    lines.append(
        f"  沪深300: close={_fmt(market.get('hs300_close'), digits=5)}"
        f"  PE_TTM={_fmt(market.get('hs300_pe_ttm'))}  PB={_fmt(market.get('hs300_pb'))}"
    )
    position = decision.position
    if position is None:
        lines.append("  仓位成因: （无仓位决策：position=None，无需归因）")
        return
    lines.append(
        f"  仓位成因: 仓位档位「{position.position_band or '—'}」的宏观约束来自市场阶段"
        f"「{_fmt(market.get('regime'))}」的建议暴露带 {band}"
        f"（市场暴露={_fmt(position.portfolio_exposure)}）——这是 market_regime 层面的"
        "仓位约束，不是个股基本面判断的降级。"
    )


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


def render_midterm_decision_summary(
    decision: CompanyStateArtifact, *, time_points: ReportTimePoints | None = None
) -> str:
    """把 ``CompanyStateArtifact`` 渲染成完整、可读、可审计的中文总结文本。

    Args:
        decision: 中期决策 artifact。
        time_points: 头部要分开标注的两个口径时点（P2-6）。缺省 ``None`` 等价于两个
            口径都「（未提供）」——**不使用决策日 / 采集日兜底**（见 :class:`ReportTimePoints`）。
    """
    tp = time_points or ReportTimePoints()
    lines: list[str] = []
    # 头部（P2-6）：财报口径 vs 估值/行情口径，两个时点分行标注。
    _render_header(lines, decision, tp)
    _render_state(lines, decision)
    _render_hypothesis(lines, decision)
    _render_expectation_gap(lines, decision)
    _render_variable_scores(lines, decision)
    _render_factor_snapshot(lines, decision)
    _render_expected_value(lines, decision)
    _render_evidence(lines, decision)
    _render_watch_and_exit(lines, decision)
    _render_market_regime(lines, decision)
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
        # P2-6：头部两个口径的时点从已采集状态字段直读（纯规则、零网络、零 LLM）。
        time_points = _extract_time_points(state)
        text = render_midterm_decision_summary(decision, time_points=time_points)
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
