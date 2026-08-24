"""Insight degradation rescue layer (ROADMAP 0.4).

四级降级阶梯（详见 docs/design/INSIGHT_DEGRADATION_DESIGN.md）：

- Tier 0: 严格解析成功（在调用方 ``nodes/insights.py`` 中先行尝试）
- Tier 1: ``lenient_parse`` — 宽松救援，只修结构不补内容，无 LLM
- Tier 2: ``build_fallback_insight`` — 确定性兜底，从 ``build_insight_context``
  的结构化 dict 直接合成观点骨架，只转述不虚构，无 LLM
- Tier 3: ``build_minimal_insight`` — 最小骨架，仅 symbol + 空字段

诚实性硬规则（H1-H4）：
- H1: 输出中每个陈述必须能在 context 里找到来源（原样或截断）
- H2: 无输入 → 无输出；bull/bear 永不虚构；无来源的 central_tension 留空
- H3: Tier>=2 时 confidence 恒为 "low"，让下游置信度调节自动生效
- H4: 不引用 thesis 层数据（synthesize_insights 先于 run_thesis 执行）
"""

from __future__ import annotations

from alphabee.agents.insights.models import (
    CrossSignalPattern,
    EvidenceItem,
    InsightOutput,
    MaterialityRank,
)
from alphabee.utils.pipeline import parse_json

# ── 枚举容错映射（补齐 models.py 里 _coerce_* 未覆盖的部分）──────────────

_WEIGHT_MAP = {
    "strong": "strong",
    "significant": "strong",
    "high": "strong",
    "moderate": "moderate",
    "medium": "moderate",
    "normal": "moderate",
    "weak": "weak",
    "low": "weak",
    "minor": "weak",
}

_IMPORTANCE_MAP = {
    "critical": "critical",
    "high": "high",
    "major": "high",
    "medium": "medium",
    "moderate": "medium",
    "normal": "medium",
    "low": "medium",
    "minor": "medium",
}

_MODIFIER_MAP = {
    "amplified": "amplified",
    "aggravated": "amplified",
    "worsened": "amplified",
    "mitigated": "mitigated",
    "softened": "mitigated",
    "unchanged": "unchanged",
    "neutral": "unchanged",
}

_CONFIDENCE_MAP = {
    "high": "high",
    "medium": "medium",
    "moderate": "medium",
    "low": "low",
    "unknown": "medium",
}

_TEXT_FIELDS = (
    "core_view",
    "central_tension",
    "main_driver",
    "business_model_context",
    "base_case",
    "bull_case",
    "bear_case",
)


def _coerce_text(value: object) -> str:
    """Coerce a value to a plain string, or ``""`` when unusable."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _coerce_enum(value: object, mapping: dict[str, str], default: str) -> str:
    if isinstance(value, str):
        return mapping.get(value.strip().lower(), default)
    return default


def _coerce_evidence_items(value: object, repairs: list[str]) -> list[dict]:
    """Accept both string items and dict items; never fail on structure."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            repairs.append("evidence item was a plain string")
            out.append({"statement": text, "source": "insight:raw", "weight": "moderate"})
        elif isinstance(item, dict):
            statement = _coerce_text(item.get("statement"))
            if not statement:
                repairs.append("evidence item missing statement")
            source = _coerce_text(item.get("source")) or "insight:raw"
            weight = _coerce_enum(item.get("weight"), _WEIGHT_MAP, "moderate")
            if (
                item.get("weight") is not None
                and _coerce_enum(item.get("weight"), _WEIGHT_MAP, "moderate")
                != str(item.get("weight", "")).strip().lower()
            ):
                repairs.append(f"evidence weight coerced: {item.get('weight')!r}")
            out.append({"statement": statement, "source": source, "weight": weight})
    return out


def _coerce_materiality(value: object, repairs: list[str]) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        variable = _coerce_text(item.get("variable"))
        importance = _coerce_enum(item.get("importance"), _IMPORTANCE_MAP, "medium")
        reasoning = _coerce_text(item.get("reasoning"))
        if (
            item.get("importance") is not None
            and _coerce_enum(item.get("importance"), _IMPORTANCE_MAP, "medium")
            != str(item.get("importance", "")).strip().lower()
        ):
            repairs.append(f"importance coerced: {item.get('importance')!r}")
        out.append({"variable": variable, "importance": importance, "reasoning": reasoning})
    return out


def _coerce_patterns(value: object, repairs: list[str]) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _coerce_text(item.get("pattern_name"))
        signals = [str(s) for s in (item.get("signals_involved") or []) if isinstance(s, str) and s.strip()]
        narrative = _coerce_text(item.get("narrative"))
        modifier = _coerce_enum(item.get("severity_modifier"), _MODIFIER_MAP, "unchanged")
        if (
            item.get("severity_modifier") is not None
            and _coerce_enum(item.get("severity_modifier"), _MODIFIER_MAP, "unchanged")
            != str(item.get("severity_modifier", "")).strip().lower()
        ):
            repairs.append(f"severity_modifier coerced: {item.get('severity_modifier')!r}")
        out.append(
            {
                "pattern_name": name,
                "signals_involved": signals,
                "narrative": narrative,
                "severity_modifier": modifier,
            }
        )
    return out


def _coerce_conditions(value: object, repairs: list[str]) -> list[str]:
    """what_would_change_my_mind: accept strings or dicts with common keys."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(text)
        elif isinstance(item, dict):
            for key in ("condition", "evidence", "statement", "falsification"):
                text = _coerce_text(item.get(key))
                if text:
                    out.append(text)
                    break
    return out


def _unwrap_nested(payload: dict) -> dict:
    """N1: 递归解包单键 dict 包装（如 ``{"insight": {...}}``）。"""
    while (
        len(payload) == 1
        and isinstance(next(iter(payload.values())), dict)
        and next(iter(payload.keys())) in {"insight", "output", "result"}
    ):
        payload = next(iter(payload.values()))
    return payload


def _normalize_payload(parsed: dict) -> tuple[dict, list[str]]:
    """一次性结构修补（规则 N1-N9），只修结构不补内容。

    Returns:
        (规范化后的 dict, 修复点列表)
    """
    repairs: list[str] = []
    payload = _unwrap_nested(parsed)

    normalized: dict = {}
    for key in _TEXT_FIELDS:
        value = _coerce_text(payload.get(key))
        if key in payload and payload.get(key) is not None and not isinstance(payload.get(key), str):
            repairs.append(f"{key} coerced to string")
        elif key not in payload or payload.get(key) in (None, ""):
            repairs.append(f"{key} missing or empty")
        normalized[key] = value

    normalized["supporting_evidence"] = _coerce_evidence_items(payload.get("supporting_evidence"), repairs)
    normalized["counter_evidence"] = _coerce_evidence_items(payload.get("counter_evidence"), repairs)
    normalized["materiality_rank"] = _coerce_materiality(payload.get("materiality_rank"), repairs)
    normalized["cross_signal_patterns"] = _coerce_patterns(payload.get("cross_signal_patterns"), repairs)
    normalized["what_would_change_my_mind"] = _coerce_conditions(payload.get("what_would_change_my_mind"), repairs)
    normalized["confidence"] = _coerce_enum(payload.get("confidence"), _CONFIDENCE_MAP, "medium")

    return normalized, repairs


def lenient_parse(raw_text: str) -> tuple[InsightOutput, str] | None:
    """Tier 1 宽松救援：结构修补后重新校验（无 LLM）。

    Returns:
        ``(修复后的 InsightOutput, 修复原因)``；完全不可解析时返回 ``None``。
    """
    try:
        parsed = parse_json(raw_text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    normalized, repairs = _normalize_payload(parsed)
    try:
        output = InsightOutput.model_validate(normalized)
    except Exception:
        return None

    reason = f"lenient_rescue: {'; '.join(dict.fromkeys(repairs))[:200]}" if repairs else "lenient_rescue"
    return output, reason


# ── Tier 2：确定性兜底（只转述 build_insight_context 的结构化 dict）──────

_LEVEL_ORDER = {"high": 3, "medium": 2, "low": 1}


def _count_level(signals: list[dict], level: str) -> int:
    return sum(1 for s in signals if str(s.get("level", "")) == level)


def _verified_conflicts(conflicts: list[dict]) -> list[dict]:
    return [
        c for c in conflicts if any(h.get("status") in ("verified", "partial") for h in (c.get("hypotheses") or []))
    ]


def _pick_central_tension(conflicts: list[dict], anomaly: dict, high_count: int) -> str:
    for conflict in _verified_conflicts(conflicts):
        theme = _coerce_text(conflict.get("theme"))
        if theme:
            return theme
    top_anomalies = anomaly.get("top_anomalies") or []
    if top_anomalies:
        metric = _coerce_text(top_anomalies[0].get("metric"))
        if metric:
            return f"{metric} 勾稽异常"
    if high_count:
        return "多个分散风险信号与基本面判断并存"
    return ""


def _pick_main_driver(key_derived: dict, signals: list[dict]) -> str:
    candidates = sorted(
        key_derived.items(),
        key=lambda kv: _LEVEL_ORDER.get(str(kv[1].get("level", "")), 0),
        reverse=True,
    )
    if candidates:
        return candidates[0][0]
    if signals:
        return str(signals[0].get("signal_id", ""))
    return ""


def _supporting_evidence(signals: list[dict]) -> list[EvidenceItem]:
    out: list[EvidenceItem] = []
    for sig in signals:
        if len(out) >= 3:
            break
        level = str(sig.get("level", ""))
        if level not in ("high", "medium"):
            continue
        interpretation = _coerce_text(sig.get("interpretation"))
        statement = interpretation or str(sig.get("signal_id", ""))
        if not statement:
            continue
        weight = "strong" if level == "high" else "moderate"
        out.append(
            EvidenceItem(
                statement=statement,
                source=f"signal:{sig.get('signal_id', '')}",
                weight=weight,
            )
        )
    return out


def _counter_evidence(conflicts: list[dict], anomaly: dict) -> list[EvidenceItem]:
    out: list[EvidenceItem] = []
    for conflict in _verified_conflicts(conflicts):
        theme = _coerce_text(conflict.get("theme"))
        for hyp in conflict.get("hypotheses") or []:
            if len(out) >= 3:
                break
            if hyp.get("status") not in ("verified", "partial"):
                continue
            summary = _coerce_text(hyp.get("summary"))
            statement = summary or _coerce_text(hyp.get("explanation"))
            if not statement:
                continue
            out.append(
                EvidenceItem(
                    statement=statement,
                    source=f"conflict:{theme or 'unknown'}",
                    weight="moderate",
                )
            )
        if len(out) >= 3:
            break
    if len(out) < 3:
        for item in (anomaly.get("top_anomalies") or [])[: 3 - len(out)]:
            metric = _coerce_text(item.get("metric"))
            if not metric:
                continue
            # H1：只转述原文，不拼接模板（"X 勾稽异常（Y）"这类组合不在上下文中）
            out.append(
                EvidenceItem(
                    statement=metric,
                    source="anomaly:top_anomalies",
                    weight="weak",
                )
            )
    return out


def _materiality_rank(key_derived: dict) -> list[MaterialityRank]:
    ranked = sorted(
        key_derived.items(),
        key=lambda kv: _LEVEL_ORDER.get(str(kv[1].get("level", "")), 0),
        reverse=True,
    )
    out: list[MaterialityRank] = []
    for name, item in ranked[:5]:
        level = str(item.get("level", ""))
        importance = "critical" if level == "high" else "high" if level == "medium" else "medium"
        reasoning = _coerce_text(item.get("interpretation"))
        if not reasoning:
            reasoning = f"衍生指标 {name} 处于 {level} 等级"
        out.append(MaterialityRank(variable=name, importance=importance, reasoning=reasoning))
    return out


def _falsification_conditions(conflicts: list[dict]) -> list[str]:
    """verified/partial 的 predictions 本身是可证伪陈述；unknown 用 gaps 补。"""
    conditions: list[str] = []
    for conflict in conflicts:
        for hyp in conflict.get("hypotheses") or []:
            if len(conditions) >= 2:
                break
            if hyp.get("status") in ("verified", "partial"):
                for prediction in (hyp.get("predictions") or [])[:1]:
                    text = _coerce_text(prediction)
                    if text:
                        conditions.append(text)
        if len(conditions) >= 2:
            break
    if len(conditions) < 2:
        for conflict in conflicts:
            for hyp in conflict.get("hypotheses") or []:
                if len(conditions) >= 4:
                    break
                if hyp.get("status") == "unknown":
                    for gap in (hyp.get("gaps") or [])[:1]:
                        text = _coerce_text(gap)
                        if text:
                            conditions.append(f"待验证: {text}")
            if len(conditions) >= 4:
                break
    return conditions[:4]


def build_fallback_insight(context: dict, symbol: str | None) -> InsightOutput:
    """Tier 2 确定性兜底：从 ``build_insight_context`` 的返回值合成观点骨架。

    只转述 context 中的结构化事实，不虚构情景与数字（H1/H2/H4）。
    """
    signals: list[dict] = context.get("key_signals") or []
    key_derived: dict = context.get("key_derived_facts") or {}
    anomaly: dict = context.get("anomaly") or {}
    conflicts: list[dict] = context.get("conflicts") or []
    company: dict = context.get("company") or {}
    snapshot: dict = context.get("latest_snapshot") or {}

    # 上下文完全没有数据时，连"未检出高风险信号"这类断言也不该输出
    # （数据缺失 ≠ 数据健康），整体返回空骨架，由调用方判定为 Tier 3。
    has_any_data = bool(signals or key_derived or anomaly or conflicts or snapshot)
    if not has_any_data:
        return InsightOutput(core_view="", central_tension="", main_driver="", confidence="low")

    high_count = _count_level(signals, "high")
    verified_conflicts = _verified_conflicts(conflicts)
    central_tension = _pick_central_tension(conflicts, anomaly, high_count)

    if high_count or verified_conflicts:
        core_view = (
            f"当前分析识别到 {high_count} 个高风险信号、{len(verified_conflicts)} 个已验证冲突，"
            f"核心矛盾聚焦于「{central_tension}」；基于现有结构化证据，投资判断需谨慎。"
        )
    else:
        core_view = "当前分析未检出高风险信号或已验证冲突，基本面未见显著恶化信号。"

    # business_model_context：由公司字段确定性拼接，缺字段则跳过
    bm_parts = []
    for label, key in (
        ("行业", "industry"),
        ("细分", "sub_industry"),
        ("生命周期", "lifecycle_stage"),
        ("市值分类", "market_cap_category"),
    ):
        value = _coerce_text(company.get(key))
        if value:
            bm_parts.append(f"{label}: {value}")
    business_model_context = "；".join(bm_parts)

    # base_case：只转述 top 信号与最新快照
    base_parts: list[str] = []
    if signals:
        top = signals[0]
        base_parts.append(
            f"核心信号为「{_coerce_text(top.get('interpretation')) or top.get('signal_id', '')}」"
            f"（{_coerce_text(top.get('level'))}）"
        )
    if snapshot:
        period = _coerce_text(snapshot.get("period"))
        revenue_yoy = snapshot.get("revenue_yoy")
        profit_yoy = snapshot.get("net_profit_yoy")
        detail = f"营收同比 {revenue_yoy}"
        if profit_yoy is not None:
            detail += f"、净利润同比 {profit_yoy}"
        base_parts.append(f"最新快照 {period or '—'}：{detail}")
    base_case = "；".join(base_parts)

    patterns: list[CrossSignalPattern] = []
    for item in (anomaly.get("pattern_matches") or [])[:3]:
        name = _coerce_text(item.get("name"))
        if not name:
            continue
        patterns.append(
            CrossSignalPattern(
                pattern_name=name,
                signals_involved=[],
                narrative=f"异常模式，严重度 {_coerce_text(item.get('severity')) or 'unknown'}",
                severity_modifier="unchanged",
            )
        )

    output = InsightOutput(
        core_view=core_view,
        central_tension=central_tension,
        main_driver=_pick_main_driver(key_derived, signals),
        supporting_evidence=_supporting_evidence(signals),
        counter_evidence=_counter_evidence(conflicts, anomaly),
        materiality_rank=_materiality_rank(key_derived),
        cross_signal_patterns=patterns,
        business_model_context=business_model_context,
        base_case=base_case,
        bull_case="",
        bear_case="",
        what_would_change_my_mind=_falsification_conditions(conflicts),
        confidence="low",
    )
    # H3 防御：兜底输出的置信度恒为 low（由调用方保证，此处再确认一次）
    return output


def build_minimal_insight(symbol: str | None, reason: str) -> InsightOutput:
    """Tier 3 最小骨架：仅保证 artifact 存在，不携带任何内容。"""
    return InsightOutput(
        core_view="",
        central_tension="",
        main_driver="",
        supporting_evidence=[],
        counter_evidence=[],
        materiality_rank=[],
        cross_signal_patterns=[],
        business_model_context="",
        base_case="",
        bull_case="",
        bear_case="",
        what_would_change_my_mind=[],
        confidence="low",
    )
