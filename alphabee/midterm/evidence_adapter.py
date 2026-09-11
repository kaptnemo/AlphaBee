"""verify_hypotheses → EvidenceEvent 适配器（E2，设计 MIDTERM_EVIDENCE_EXTRACTION.md §9/§12）。

复用已有 ``verify_hypotheses`` agent 的「假设 × 证据验证」结构化输出
（:class:`alphabee.agents.schemas.VerificationResultItem`），把它映射为
:class:`EvidenceEvent`。**零新 LLM 链路**：本模块只做结构映射，不调 LLM、不碰
分数/状态/仓位计算。

映射规则（§9）：

- **方向**：``status``（验证结论）→ ``effect_on_thesis``——``verified``→confirming、
  ``rejected``→refuting、``partial``→neutral；``unknown``（无结论）丢弃。
- **强度**：``confidence``（0-1 连续）→ 离散等级（§6 定性类禁连续值）——
  ``>=0.7``→strong / ``>=0.4``→medium / 否则 weak；经 ``STRENGTH_DELTA`` 映射为
  0.5 / 0.3 / 0.1。neutral 事件无方向证据，``confidence_delta=0.0``（bayes 对
  neutral 恒返回 LR=1，不参与更新，非离散强度等级、更非连续值标定）。
- **引用**：``supporting_evidence + refuting_evidence`` → ``source_refs``。
- **防幻觉**：无引用（无 supporting/refuting evidence）或无结论（unknown）的
  结果丢弃（§3 铁律「无引用不出数」）。

纪律（alphabee-schema-steward / alphabee-pipeline-contract-steward）：

- 只读 ``VerificationResultItem`` 的 canonical 字段，不引入外部字段名；
- ``confidence_delta`` 只允许 weak=0.1 / medium=0.3 / strong=0.5（上限 0.7）；
- 事件签名去重 ``id=hash(date+kind+主体+数值)``（与 ``evidence_rules`` 同格式，§7）。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from alphabee.midterm.models import (
    STRENGTH_DELTA,
    EffectOnThesis,
    EvidenceEvent,
    Strength,
)

_KIND = "thesis"  # verify_hypotheses 验证的是 thesis 相关假设

# status（验证结论）→ effect_on_thesis（§9）
_VERDICT_TO_EFFECT: dict[str, EffectOnThesis] = {
    "verified": EffectOnThesis.CONFIRMING,
    "rejected": EffectOnThesis.REFUTING,
    "partial": EffectOnThesis.NEUTRAL,
    "unknown": EffectOnThesis.NEUTRAL,
}

# verify_hypotheses 的连续 confidence (0-1) → 离散等级阈值（§6，示意需回测）
_CONFIDENCE_STRONG = 0.7  # >= 0.7 → strong
_CONFIDENCE_MEDIUM = 0.4  # >= 0.4 → medium；否则 weak


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────


def _to_float(value: Any) -> float | None:
    """归一化为 float；None/空串/NaN/inf/无效值 → None（防幻觉：缺失不编造）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    else:
        text = str(value).strip()
        if text in ("", "-", "--", "None", "null", "nan", "NaN"):
            return None
        try:
            f = float(text)
        except (TypeError, ValueError):
            return None
    return f if math.isfinite(f) else None


def _discrete_strength(confidence: Any) -> Strength:
    """连续 confidence → 离散等级（§6 定性类禁连续值）；缺失 → 默认 weak（§11 降级）。"""
    c = _to_float(confidence)
    if c is None:
        return Strength.WEAK
    if c >= _CONFIDENCE_STRONG:
        return Strength.STRONG
    if c >= _CONFIDENCE_MEDIUM:
        return Strength.MEDIUM
    return Strength.WEAK


def _strength_to_delta(strength: Strength) -> float:
    """离散等级 → confidence_delta 数值（唯一映射，禁连续值）。"""
    return STRENGTH_DELTA[strength.value]


def _event_id(date: str, kind: str, subject: str, value: float | None) -> str:
    """事件签名哈希（§7，与 ``evidence_rules._event_id`` 同格式）。"""
    v = "" if value is None else f"{value:g}"
    raw = f"{date}|{kind}|{subject}|{v}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dedup(events: list[EvidenceEvent]) -> list[EvidenceEvent]:
    """按 ``id`` 去重（§7）：同 id 合并 source_refs，同主题只算一次。"""
    merged: dict[str, EvidenceEvent] = {}
    for ev in events:
        if ev.id in merged:
            prev = merged[ev.id]
            refs = sorted(set(prev.source_refs) | set(ev.source_refs))
            merged[ev.id] = prev.model_copy(update={"source_refs": refs})
        else:
            merged[ev.id] = ev
    return list(merged.values())


# ─────────────────────────────────────────────────────────────────────────────
# 输入归一化（接受 list / 单个 dict / VerificationResultList / VerificationArtifact）
# ─────────────────────────────────────────────────────────────────────────────


def _coerce_items(verification: Any) -> list[dict[str, Any]]:
    """把多种输入形态统一为 verification result dict 列表。

    接受：``list[VerificationResultItem]`` / ``list[dict]`` / 单个 item / 单个 dict /
    带 ``.results`` 的对象（``VerificationResultList`` / ``VerificationArtifact``）。
    """
    if verification is None:
        return []

    # 对象带 .results（VerificationResultList / VerificationArtifact）
    if hasattr(verification, "results"):
        verification = verification.results

    # 单个 dict → 可能是 {"results": [...]} 或单个结果 dict
    if isinstance(verification, dict):
        inner = verification.get("results")
        if isinstance(inner, list):
            verification = inner
        else:
            verification = [verification]

    # 单个 pydantic model → 包成 list
    if not isinstance(verification, (list, tuple)):
        verification = [verification]

    items: list[dict[str, Any]] = []
    for item in verification:
        if isinstance(item, dict):
            items.append(item)
        elif hasattr(item, "model_dump"):
            items.append(item.model_dump())
        else:
            items.append(
                {
                    k: getattr(item, k, None)
                    for k in (
                        "id",
                        "hypothesis_id",
                        "status",
                        "support_score",
                        "contradiction_score",
                        "confidence",
                        "supporting_evidence",
                        "refuting_evidence",
                        "gaps",
                        "summary",
                    )
                }
            )
    return items


def _str_list(value: Any) -> list[str]:
    """归一化为非空字符串列表（去重保序）。"""
    if not isinstance(value, (list, tuple)):
        value = [value] if value is not None else []
    out: list[str] = []
    seen: set[str] = set()
    for v in value:
        s = str(v).strip()
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 单条映射
# ─────────────────────────────────────────────────────────────────────────────


def _map_item(item: dict[str, Any], symbol: str, date: str) -> EvidenceEvent | None:
    """单条 VerificationResultItem → EvidenceEvent（无引用/无结论 → None）。"""
    status = str(item.get("status") or "").strip().lower()
    if status == "unknown":
        return None  # 无结论（§9）：证据未闭环，丢弃

    # 无引用不出数（§3 铁律）：supporting/refuting evidence 全空 → 丢弃
    source_refs = [
        *_str_list(item.get("supporting_evidence")),
        *_str_list(item.get("refuting_evidence")),
    ]
    if not source_refs:
        return None

    effect = _VERDICT_TO_EFFECT.get(status, EffectOnThesis.NEUTRAL)
    hypothesis_id = str(item.get("hypothesis_id") or item.get("id") or "").strip()
    summary = str(item.get("summary") or "").strip()
    confidence = _to_float(item.get("confidence"))

    description = summary or f"假设 {hypothesis_id} 验证结论：{status}"
    subject = f"{symbol}:verify:{hypothesis_id}" if hypothesis_id else f"{symbol}:verify"

    if effect is EffectOnThesis.NEUTRAL:
        # partial → neutral：无方向证据，delta=0.0（bayes no-op，非连续值标定）
        confidence_delta = 0.0
    else:
        confidence_delta = _strength_to_delta(_discrete_strength(confidence))

    return EvidenceEvent(
        id=_event_id(date, _KIND, subject, confidence),
        date=date,
        kind=_KIND,
        description=description,
        effect_on_thesis=effect,
        confidence_delta=confidence_delta,
        source_refs=source_refs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def adapt_verification(
    verification: Any = None,
    *,
    symbol: str = "",
    date: str = "",
) -> list[EvidenceEvent]:
    """把 verify_hypotheses 的结构化验证输出映射为 EvidenceEvent[]（零新 LLM 链路）。

    Args:
        verification: verify_hypotheses 的输出——``list[VerificationResultItem]`` /
            ``list[dict]`` / 单个 item / ``VerificationResultList`` /
            ``VerificationArtifact``（均含 ``results``）。
        symbol: 股票代码（用于事件签名主体）。
        date: 事件发生日 YYYY-MM-DD（验证结果无日期字段，需调用方传入）。

    Returns:
        去重后的 EvidenceEvent[]（confirming/refuting 的 confidence_delta 只取
        0.1/0.3/0.5；partial 为 neutral 且 delta=0.0；无引用/无结论结果丢弃）。
    """
    items = _coerce_items(verification)
    events = [ev for item in items if (ev := _map_item(item, symbol, date)) is not None]
    return _dedup(events)
