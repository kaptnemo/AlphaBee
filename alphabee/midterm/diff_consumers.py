"""CompanyStateDiff 的消费方接线（design MIDTERM_STATE_DIFF_DESIGN.md §9）。

把 diff 引擎的产物 :class:`~alphabee.midterm.models.CompanyStateDiff` 接到四个下游，
杜绝 dead-end artifact（alphabee-pipeline-contract-steward）：

- ``check_exit(diff)``：ExitEngine 检查投影（§37）——``exit_conditions_met`` +
  ``StateShift.kind=downgrade`` + ``PositionDiff.band_weight_divergence`` → 退出信号；
- ``project_journal(diff)``：决策日志投影——``thesis_delta`` + ``attribution`` →
  :class:`DecisionJournalEntry`（"当时为什么动/不动"）；
- ``project_narrative(diff)``：报告叙事投影——``thesis_delta`` + ``attribution.note``
  → 可读叙述（替代"当前评分78"）；
- ``monitor_triggers(diff)``：监控触发器（§36 EVI）——``tv_distance`` /
  ``evidence_arrival_rate`` 超阈值 → 触发深度研究；
- ``anchor_diff(entry, curr)``：锚点 diff 便捷入口（§44）——复用 ``diff(prev=entry)``。

纪律：本模块全部为确定性投影，数值核心禁 LLM（阈值比较 / 字段投影均为纯规则）；
叙述由 diff 的模板 ``thesis_delta`` / ``attribution.note`` 投影而来，LLM 润色在更上
游的报告层（若需要）做。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from alphabee.midterm.diff import diff
from alphabee.midterm.models import CompanyStateArtifact, CompanyStateDiff, DecisionJournalEntry

# 监控触发器阈值（结构性示意，§36 EVI；应回测调参）
_TV_TRIGGER = 0.3  # 信念位移总量（tv_distance，0-1）超阈值 → 高信息量变化
_EVIDENCE_RATE_TRIGGER = 0.5  # 证据到达率（条/交易日）超阈值 → 触发深度研究


class ExitSignal(BaseModel):
    """ExitEngine 检查投影结果。"""

    should_exit: bool = False
    reasons: list[str] = Field(default_factory=list)


class MonitorTrigger(BaseModel):
    """监控触发器结果（§36 EVI：高信息量变化 → 触发深度研究）。"""

    triggered: bool = False
    triggers: list[str] = Field(default_factory=list)


def check_exit(d: CompanyStateDiff) -> ExitSignal:
    """ExitEngine 检查投影（§37 五层退出检查的 diff 侧信号，纯规则）。

    退出信号 = 新满足的退出条件 或 状态降级 或 仓位带背离。
    ``thesis_broken`` 的语义判断在上游（EvidenceEvent 层）做，这里只投影数值信号。
    """
    reasons: list[str] = []
    if d.exit_conditions_met:
        reasons.append(f"退出条件触发：{','.join(d.exit_conditions_met)}")
    if d.state_shift is not None and d.state_shift.kind == "downgrade":
        reasons.append(f"状态降级：{d.state_shift.argmax_from}→{d.state_shift.argmax_to}")
    if d.position is not None and d.position.band_weight_divergence:
        reasons.append("仓位带与实际仓位背离")
    return ExitSignal(should_exit=bool(reasons), reasons=reasons)


def _journal_action(d: CompanyStateDiff) -> str:
    """由 diff 投影决策动作（buy/add/hold/reduce/sell/replace）。"""
    if d.exit_conditions_met:
        return "sell"
    if d.state_shift is not None and d.state_shift.kind == "downgrade":
        return "reduce"
    if d.position is not None and d.position.band_weight_divergence:
        return "reduce"
    return "hold"


def project_journal(d: CompanyStateDiff) -> DecisionJournalEntry:
    """决策日志投影：``thesis_delta`` + ``attribution`` → :class:`DecisionJournalEntry`。"""
    rationale = [a.note for a in d.attribution if a.note]
    evidence_refs = [eid for a in d.attribution for eid in a.evidence_ids]
    posterior = d.confidence.posterior if d.confidence is not None else None
    return DecisionJournalEntry(
        id=f"journal:{d.symbol}:{d.curr.date}",
        date=d.curr.date,
        action=_journal_action(d),
        symbol=d.symbol,
        rationale=rationale,
        thesis_at_time=d.thesis_delta,
        confidence_at_time=posterior if posterior is not None else 0.0,
        evidence_refs=evidence_refs,
        review_notes=list(d.exit_conditions_met),
    )


def project_narrative(d: CompanyStateDiff) -> str:
    """报告叙事投影：``thesis_delta`` + ``attribution.note`` → 可读叙述。

    替代"当前评分78"式快照叙述，输出"为什么变"（§35）。确定性模板句，LLM 润色留上游。
    """
    header = f"{d.symbol} @ {d.curr.date}"
    state_part = ""
    if d.state_shift is not None and d.state_shift.argmax_from != d.state_shift.argmax_to:
        state_part = f"（状态 {d.state_shift.argmax_from}→{d.state_shift.argmax_to}）"
    body = d.thesis_delta or "无显著变化"
    return f"{header}：{body}{state_part}"


def monitor_triggers(
    d: CompanyStateDiff,
    *,
    tv_threshold: float = _TV_TRIGGER,
    evidence_rate_threshold: float = _EVIDENCE_RATE_TRIGGER,
) -> MonitorTrigger:
    """监控触发器（§36 EVI）：``tv_distance`` / ``evidence_arrival_rate`` 超阈值 → 深度研究。

    ``evidence_arrival_rate`` 由单帧 raw 量派生（``len(new_evidence)/elapsed_days``，
    与 ``diff_series`` 同口径），不落单帧（避免单帧自指）。
    """
    triggers: list[str] = []
    tv = d.state_shift.tv_distance if d.state_shift is not None else 0.0
    if tv > tv_threshold:
        triggers.append(f"tv_distance={tv:.3f}>{tv_threshold}")
    if d.elapsed_days > 0:
        rate = len(d.new_evidence) / d.elapsed_days
        if rate > evidence_rate_threshold:
            triggers.append(f"evidence_arrival_rate={rate:.3f}>{evidence_rate_threshold}")
    return MonitorTrigger(triggered=bool(triggers), triggers=triggers)


def anchor_diff(entry: CompanyStateArtifact, curr: CompanyStateArtifact) -> CompanyStateDiff:
    """锚点 diff 便捷入口（§44）：以建仓帧为 ``prev`` 复用同一 diff 引擎。

    回答"相对建仓时证据/预期/赔率变了什么"，并把 ``anchor`` 引用记入 diff。
    """
    return diff(entry, curr, anchor=entry)
