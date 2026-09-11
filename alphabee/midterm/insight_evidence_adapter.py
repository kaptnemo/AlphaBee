"""InsightArtifact → EvidenceEvent 适配器（改造 A，设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）。

把洞察层产出的结构化正反证据（``InsightArtifact.supporting_evidence`` /
``counter_evidence``，每项 ``{statement, source, weight}``）映射为
:class:`EvidenceEvent`，让「高速通信线+35.44%」「增收不增利」这类结构性洞察成为
中期决策证据日志的一等来源——不再被 ``resolve_midterm_decision`` 边界降维丢弃。

**零新 LLM 链路**：本模块只做结构映射（确定性纯函数），不调 LLM、不碰
分数/状态/仓位计算。LLM 边界不变：洞察层（InsightAgent）产出结构化证据，
本适配器与 midterm 核心只消费。

映射规则（§4 改造 A）：

- **方向**：``supporting_evidence`` → ``confirming``；``counter_evidence`` → ``refuting``。
- **强度**：``weight``（离散等级）→ 强度——``strong``→strong（0.5）、
  ``moderate``→medium（0.3）、``weak``→weak（0.1）；缺失/未知默认 ``moderate``
  （与 ``EvidenceItem.weight`` 默认口径一致）。禁连续值。
- **引用**：``source`` → ``source_refs``（可能为空：``statement`` 本身即证据载体，
  与 verify 适配器的「无引用不出数」铁律口径不同——insight 证据是 LLM 观点层
  已带权重标注的结构化陈述）。
- **防幻觉**：``statement`` 为空 → 丢弃（无描述不出事件）。
- **去重**：``id=hash(date+kind+主体)``（与 ``evidence_rules`` / ``evidence_adapter``
  同格式，§7），同陈述同事件只算一次。

纪律（alphabee-schema-steward / alphabee-pipeline-contract-steward）：

- 只读 ``InsightArtifact`` 的 canonical 字段（``supporting_evidence`` /
  ``counter_evidence``），不引入外部字段名；
- ``confidence_delta`` 只允许 weak=0.1 / medium=0.3 / strong=0.5（上限 0.7）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from alphabee.midterm.models import (
    STRENGTH_DELTA,
    EffectOnThesis,
    EvidenceEvent,
    Strength,
)

_KIND = "thesis"  # insight 的正反证据都是围绕 thesis/core_view 的方向判定

# weight（离散）→ 强度等级（§4 改造 A；禁连续值）
_WEIGHT_TO_STRENGTH: dict[str, Strength] = {
    "strong": Strength.STRONG,
    "moderate": Strength.MEDIUM,
    "weak": Strength.WEAK,
}

# 缺失/未知 weight 的默认等级（与 EvidenceItem.weight 默认 "moderate" 口径一致）
_DEFAULT_WEIGHT = "moderate"


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────


def _str_value(value: Any) -> str:
    """归一化为字符串；``None`` → 空串（缺失显式空，不编造）。"""
    return str(value).strip() if value is not None else ""


def _event_id(date: str, kind: str, subject: str, value: float | None = None) -> str:
    """事件签名哈希（§7，与 ``evidence_rules._event_id`` 同格式）。"""
    v = "" if value is None else f"{value:g}"
    raw = f"{date}|{kind}|{subject}|{v}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dedup(events: list[EvidenceEvent]) -> list[EvidenceEvent]:
    """按 ``id`` 去重（§7）：同 id 合并 ``source_refs``，同陈述只算一次。"""
    merged: dict[str, EvidenceEvent] = {}
    for ev in events:
        if ev.id in merged:
            prev = merged[ev.id]
            refs = sorted(set(prev.source_refs) | set(ev.source_refs))
            merged[ev.id] = prev.model_copy(update={"source_refs": refs})
        else:
            merged[ev.id] = ev
    return list(merged.values())


def _coerce_items(evidence: Any) -> list[dict[str, Any]]:
    """把多种输入形态统一为证据 dict 列表（list[dict] / list[EvidenceItem] / 单条）。"""
    if evidence is None:
        return []
    if isinstance(evidence, dict):
        evidence = [evidence]
    elif not isinstance(evidence, (list, tuple)):
        evidence = [evidence]

    items: list[dict[str, Any]] = []
    for item in evidence:
        if isinstance(item, dict):
            items.append(item)
        elif hasattr(item, "model_dump"):
            items.append(item.model_dump())
        else:
            items.append({})  # 未知形态 → 空 dict，后续因 statement 为空被丢弃
    return items


def _map_item(
    item: dict[str, Any],
    *,
    direction: EffectOnThesis,
    date: str,
    symbol: str,
) -> EvidenceEvent | None:
    """单条 insight 证据 → EvidenceEvent（statement 为空 → None）。"""
    statement = _str_value(item.get("statement"))
    if not statement:
        return None  # 防幻觉：无陈述不出事件

    weight = _str_value(item.get("weight")).lower()
    strength = _WEIGHT_TO_STRENGTH.get(weight, _WEIGHT_TO_STRENGTH[_DEFAULT_WEIGHT])

    source = _str_value(item.get("source"))
    source_refs = [source] if source else []

    subject = f"{symbol}:insight:{statement}" if symbol else f"insight:{statement}"

    return EvidenceEvent(
        id=_event_id(date, _KIND, subject),
        date=date,
        kind=_KIND,
        description=statement,
        effect_on_thesis=direction,
        confidence_delta=STRENGTH_DELTA[strength.value],
        source_refs=source_refs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def adapt_insight_evidence(
    insight: Any = None,
    *,
    symbol: str = "",
    date: str = "",
) -> list[EvidenceEvent]:
    """把 ``InsightArtifact`` 的正反证据映射为 EvidenceEvent[]（纯函数，零新 LLM 链路）。

    Args:
        insight: ``InsightArtifact``（``alphabee.orchestrator.contracts``），或含
            ``supporting_evidence`` / ``counter_evidence`` 的 dict / 对象。
        symbol: 股票代码（用于事件签名主体）。
        date: 事件发生日 YYYY-MM-DD（insight 证据无日期字段，需调用方传入）。

    Returns:
        去重后的 EvidenceEvent[]：supporting → confirming、counter → refuting；
        ``confidence_delta`` 只取 0.1 / 0.3 / 0.5；``statement`` 为空的证据丢弃。
    """
    if insight is None:
        return []

    if isinstance(insight, dict):
        supporting = insight.get("supporting_evidence")
        counter = insight.get("counter_evidence")
    else:
        supporting = getattr(insight, "supporting_evidence", None)
        counter = getattr(insight, "counter_evidence", None)

    events: list[EvidenceEvent] = []
    for item in _coerce_items(supporting):
        ev = _map_item(item, direction=EffectOnThesis.CONFIRMING, date=date, symbol=symbol)
        if ev is not None:
            events.append(ev)
    for item in _coerce_items(counter):
        ev = _map_item(item, direction=EffectOnThesis.REFUTING, date=date, symbol=symbol)
        if ev is not None:
            events.append(ev)
    return _dedup(events)
