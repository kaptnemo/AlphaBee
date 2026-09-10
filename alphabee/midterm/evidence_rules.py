"""数值类 EvidenceEvent 规则生成器（E1，设计 MIDTERM_EVIDENCE_EXTRACTION.md §6 / §12）。

纯规则禁 LLM：把结构化数值事实（业绩预告 forecast / 业绩快报 express / 盈利预测
修正 revision）翻译为 :class:`EvidenceEvent`，供 ``bayes.update_confidence`` 消费。
本模块是确定性纯函数：不调 LLM、不读非结构化文本、不碰分数/状态/仓位计算。

规则（§6 表，全部离散，禁连续值）：

- **beat/miss**（express 实际 vs forecast 预告区间）：实际净利润同比超出预告上限
  → confirming，偏离幅度 <5%→weak / 5–15%→medium / >15%→strong；低于预告下限
  → refuting（同幅度表）；落在区间内 → neutral（无意外即无证据，不产事件）。
- **revision 幅度**（consensus 上/下修）：方向=符号（>0 confirming / <0 refuting），
  |Δ|<3%→weak / 3–10%→medium / >10%→strong。
- **符号定方向退化**（§8，thesis 为空实跑现状）：无预告基线时，快报/预告同比按符号
  定方向，幅度复用 §6 超上限幅度表（相对上年 0 基线的偏离）。

纪律（alphabee-schema-steward / alphabee-pipeline-contract-steward）：

- 数值必须来自结构化字段（canonical），缺失/冲突置 ``None``，绝不静默回退为 0 或编造；
- ``confidence_delta`` 只允许 weak=0.1 / medium=0.3 / strong=0.5 三个离散值（上限 0.7）；
- 事件签名去重 ``id=hash(date+kind+主体+数值)``（§7），多来源合并 source_refs、同主题只算一次。
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

# ─────────────────────────────────────────────────────────────────────────────
# 离散标定阈值（§6，禁连续值）
# ─────────────────────────────────────────────────────────────────────────────

_BEAT_WEAK_MAX = 5.0  # 盈利超出预告幅度 <5% → weak
_BEAT_MEDIUM_MAX = 15.0  # 5–15% → medium；>15% → strong

_REVISION_WEAK_MAX = 3.0  # |revision| <3% → weak
_REVISION_MEDIUM_MAX = 10.0  # 3–10% → medium；>10% → strong

# 数值类证据统一受控 kind（§3：fundamental / expectation / trend / crowding / thesis / price）
_KIND = "expectation"

# revision 幅度字段（consensus canonical，§6「revision_1m 幅度」）
_REVISION_FIELDS: tuple[str, ...] = (
    "eps_fy1_revision_1m",
    "eps_fy1_revision_3m",
    "eps_fy2_revision_1m",
)

_REVISION_LABELS: dict[str, str] = {
    "eps_fy1_revision_1m": "FY1 EPS 近1月",
    "eps_fy1_revision_3m": "FY1 EPS 近3月",
    "eps_fy2_revision_1m": "FY2 EPS 近1月",
}


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────


def _to_float(value: Any) -> float | None:
    """把结构化数值归一为 float；空串/None/NaN/inf/无效值 → None（防幻觉：缺失不编造）。"""
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


def _norm_date(value: Any) -> str:
    """归一化日期为 ``YYYY-MM-DD``（tushare ``ann_date``/``end_date`` 为 ``YYYYMMDD``）。"""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        d = digits[:8]
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return s


def _effect_from_sign(value: float) -> EffectOnThesis:
    """数值符号 → 方向（§8 thesis 为空时的退化）：>0 confirming / <0 refuting / ==0 neutral。"""
    if value > 0.0:
        return EffectOnThesis.CONFIRMING
    if value < 0.0:
        return EffectOnThesis.REFUTING
    return EffectOnThesis.NEUTRAL


def _calibrate_beat(deviation: float) -> Strength:
    """盈利超出预告幅度离散标定（§6）：<5 weak / 5–15 medium / >15 strong。"""
    d = abs(deviation)
    if d < _BEAT_WEAK_MAX:
        return Strength.WEAK
    if d <= _BEAT_MEDIUM_MAX:
        return Strength.MEDIUM
    return Strength.STRONG


def _calibrate_revision(delta: float) -> Strength:
    """revision 幅度离散标定（§6）：|Δ|<3 weak / 3–10 medium / >10 strong。"""
    d = abs(delta)
    if d < _REVISION_WEAK_MAX:
        return Strength.WEAK
    if d <= _REVISION_MEDIUM_MAX:
        return Strength.MEDIUM
    return Strength.STRONG


def _strength_to_delta(strength: Strength) -> float:
    """离散等级 → confidence_delta 数值（唯一映射，禁连续值）。"""
    return STRENGTH_DELTA[strength.value]


def _event_id(date: str, kind: str, subject: str, value: float | None) -> str:
    """事件签名哈希（§7）：``id = hash(date+kind+主体+数值)``。"""
    v = "" if value is None else f"{value:g}"
    raw = f"{date}|{kind}|{subject}|{v}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dedup(events: list[EvidenceEvent]) -> list[EvidenceEvent]:
    """按 ``id`` 去重（§7）：同 id 合并 ``source_refs``，同主题只算一次。"""
    merged: dict[str, EvidenceEvent] = {}
    for ev in events:
        if ev.id in merged:
            prev = merged[ev.id]
            refs = sorted(set(prev.source_refs) | set(ev.source_refs))
            merged[ev.id] = prev.model_copy(update={"source_refs": refs})
        else:
            merged[ev.id] = ev
    return list(merged.values())


def _make_event(
    *,
    date: str,
    subject: str,
    value: float | None,
    description: str,
    effect: EffectOnThesis,
    strength: Strength,
    source_refs: list[str],
) -> EvidenceEvent:
    """组装一条 EvidenceEvent（confidence_delta 只取离散等级映射值）。"""
    return EvidenceEvent(
        id=_event_id(date, _KIND, subject, value),
        date=date,
        kind=_KIND,
        description=description,
        effect_on_thesis=effect,
        confidence_delta=_strength_to_delta(strength),
        source_refs=source_refs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# beat/miss（§6 ①）
# ─────────────────────────────────────────────────────────────────────────────


def _classify_beat(
    actual: float,
    lo: float,
    hi: float,
) -> tuple[EffectOnThesis | None, float | None]:
    """beat/miss 判定：实际值 vs 预告区间 [lo, hi]（前提：lo/hi 有效且 lo <= hi）。

    - ``actual > hi`` → confirming，偏离幅度 = actual − hi；
    - ``actual < lo`` → refuting，偏离幅度 = lo − actual；
    - ``lo <= actual <= hi`` → neutral（无意外，不产事件）→ ``(None, None)``。
    """
    if actual > hi:
        return EffectOnThesis.CONFIRMING, actual - hi
    if actual < lo:
        return EffectOnThesis.REFUTING, lo - actual
    return None, None  # inline → neutral


def _index_by_period(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 ``period`` 建索引（取首条，避免同报告期重复）。"""
    out: dict[str, dict[str, Any]] = {}
    for rec in records or []:
        p = str(rec.get("period") or "").strip()
        if p and p not in out:
            out[p] = rec
    return out


def _midpoint(lo: float | None, hi: float | None) -> float | None:
    """预告区间中点（单边缺失时退化为另一侧）；全缺失 → None。"""
    if lo is not None and hi is not None:
        return (lo + hi) / 2.0
    if lo is not None:
        return lo
    return hi


def _express_source(symbol: str) -> str:
    return f"tushare:express:{symbol}" if symbol else "tushare:express"


def _forecast_source(symbol: str) -> str:
    return f"tushare:forecast:{symbol}" if symbol else "tushare:forecast"


def _revision_source(symbol: str) -> str:
    return f"eastmoney:consensus:{symbol}" if symbol else "eastmoney:consensus"


def _beat_description(actual: float, lo: float, hi: float, effect: EffectOnThesis) -> str:
    if effect is EffectOnThesis.CONFIRMING:
        return f"业绩快报净利润同比 {actual:+.2f}%，超出预告上限 {hi:+.2f}%"
    return f"业绩快报净利润同比 {actual:+.2f}%，低于预告下限 {lo:+.2f}%"


def _forecast_description(lo: float | None, hi: float | None) -> str:
    if lo is not None and hi is not None:
        return f"业绩预告净利润同比区间 [{lo:+.2f}%, {hi:+.2f}%]"
    v = lo if lo is not None else hi
    return f"业绩预告净利润同比 {v:+.2f}%"


def _revision_description(field: str, value: float) -> str:
    label = _REVISION_LABELS.get(field, field)
    direction = "上修" if value > 0 else "下修"
    return f"{label} {direction} {abs(value):.2f}%"


# ─────────────────────────────────────────────────────────────────────────────
# 三类证据生产者
# ─────────────────────────────────────────────────────────────────────────────


def _express_evidence(
    express_records: list[dict[str, Any]],
    forecast_records: list[dict[str, Any]],
    symbol: str,
) -> tuple[list[EvidenceEvent], set[str]]:
    """快报实际值 → beat/miss（有预告）或符号定方向（无预告）证据。

    返回 ``(events, covered_periods)``；``covered_periods`` 为已产事件的报告期集合，
    供 forecast 侧跳过，避免同一报告期重复计证据（§7 同主题只算一次）。
    """
    forecast_by_period = _index_by_period(forecast_records)
    events: list[EvidenceEvent] = []
    covered: set[str] = set()

    for rec in express_records or []:
        period = str(rec.get("period") or "").strip()
        actual = _to_float(rec.get("express_net_profit_yoy"))
        if actual is None:
            continue  # 防幻觉：无实际同比数值，不产事件（预告仍可单独计）
        # 只要有实际值，该报告期即被快报覆盖（即使 inline 无事件），预告不重复计（§7 同主题只算一次）
        covered.add(period)
        date = _norm_date(rec.get("ann_date")) or _norm_date(rec.get("period"))

        fc = forecast_by_period.get(period) if period else None
        lo = _to_float(fc.get("profit_forecast_min_change")) if fc is not None else None
        hi = _to_float(fc.get("profit_forecast_max_change")) if fc is not None else None

        # 预告区间有效 → beat/miss（inline 无意外不产事件）
        if fc is not None and lo is not None and hi is not None and lo <= hi:
            effect, dev = _classify_beat(actual, lo, hi)
            if effect is not None and dev is not None:
                events.append(
                    _make_event(
                        date=date,
                        subject=f"{symbol}:beat:{period}",
                        value=actual,
                        description=_beat_description(actual, lo, hi, effect),
                        effect=effect,
                        strength=_calibrate_beat(dev),
                        source_refs=[_express_source(symbol), _forecast_source(symbol)],
                    )
                )
            # inline（actual ∈ [lo, hi]）→ neutral，不产事件、不降级为符号方向
            continue

        # 无预告 / 预告区间缺失或冲突（lo>hi）→ 符号定方向（§5 冲突降级只认实际值，§8）
        effect = _effect_from_sign(actual)
        if effect is EffectOnThesis.NEUTRAL:
            continue
        events.append(
            _make_event(
                date=date,
                subject=f"{symbol}:express:{period}",
                value=actual,
                description=f"业绩快报净利润同比 {actual:+.2f}%",
                effect=effect,
                strength=_calibrate_beat(actual),
                source_refs=[_express_source(symbol)],
            )
        )

    return events, covered


def _forecast_evidence(
    forecast_records: list[dict[str, Any]],
    covered_periods: set[str],
    symbol: str,
) -> list[EvidenceEvent]:
    """业绩预告（无对应快报）→ 预告方向证据（符号定方向，幅度用预告同比）。"""
    events: list[EvidenceEvent] = []
    for rec in forecast_records or []:
        period = str(rec.get("period") or "").strip()
        if period in covered_periods:
            continue  # 已有快报实际值 → beat/miss 已覆盖，同主题不重复计
        lo = _to_float(rec.get("profit_forecast_min_change"))
        hi = _to_float(rec.get("profit_forecast_max_change"))
        midpoint = _midpoint(lo, hi)
        if midpoint is None:
            continue  # 防幻觉：无预告数值，不产事件
        effect = _effect_from_sign(midpoint)
        if effect is EffectOnThesis.NEUTRAL:
            continue
        date = _norm_date(rec.get("ann_date")) or _norm_date(rec.get("period"))
        events.append(
            _make_event(
                date=date,
                subject=f"{symbol}:forecast:{period}",
                value=midpoint,
                description=_forecast_description(lo, hi),
                effect=effect,
                strength=_calibrate_beat(midpoint),
                source_refs=[_forecast_source(symbol)],
            )
        )
    return events


def _revision_evidence(
    values: dict[str, Any],
    symbol: str,
    as_of_date: str,
) -> list[EvidenceEvent]:
    """分析师盈利预测修正 → 方向证据（方向=符号，幅度离散标定 §6）。"""
    events: list[EvidenceEvent] = []
    for field in _REVISION_FIELDS:
        v = _to_float(values.get(field))
        if v is None:
            continue  # 防幻觉：无修正数值，不产事件
        effect = _effect_from_sign(v)
        if effect is EffectOnThesis.NEUTRAL:
            continue
        events.append(
            _make_event(
                date=as_of_date,
                subject=f"{symbol}:revision:{field}",
                value=v,
                description=_revision_description(field, v),
                effect=effect,
                strength=_calibrate_revision(v),
                source_refs=[_revision_source(symbol)],
            )
        )
    return events


def _consensus_values(consensus: Any) -> dict[str, Any]:
    """归一化 consensus 输入：接受 ``ConsensusOutput``（含 ``.values``）或 plain dict。

    注意：必须优先判 ``dict``——dict 自带 ``.values`` 方法，若先判 ``hasattr(values)``
    会把 bound method 误当数据。
    """
    if consensus is None:
        return {}
    if isinstance(consensus, dict):
        inner = consensus.get("values")
        return inner if isinstance(inner, dict) else consensus
    if hasattr(consensus, "values"):
        return consensus.values or {}
    return {}


def _consensus_date(consensus: Any) -> str:
    if consensus is None:
        return ""
    if isinstance(consensus, dict):
        return str(consensus.get("as_of_date") or "")
    if hasattr(consensus, "as_of_date"):
        return consensus.as_of_date or ""
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def build_numeric_evidence(
    exp_data: dict[str, Any] | None = None,
    consensus: Any = None,
    *,
    symbol: str = "",
    as_of_date: str = "",
) -> list[EvidenceEvent]:
    """数值类 EvidenceEvent 规则生成器主入口（纯规则，禁 LLM）。

    Args:
        exp_data: ``get_expectation_fact(symbol)`` 的结构化输出，含 ``forecast`` /
            ``express`` 两个 canonical 记录列表（业绩预告 / 业绩快报）。
        consensus: ``build_consensus(symbol)`` 的 ``ConsensusOutput``（或含 ``values``
            的 dict / 直接含 revision canonical 字段的 dict）。
        symbol: 股票代码（用于事件签名主体与 source_refs）。
        as_of_date: revision 事件的发生日（YYYY-MM-DD）；缺省取 ``consensus.as_of_date``。

    Returns:
        去重后的 EvidenceEvent[]（confirming / refuting；confidence_delta 只取离散等级
        0.1 / 0.3 / 0.5，禁连续值；neutral 不产事件）。
    """
    data = exp_data or {}
    forecast_records = data.get("forecast") or []
    express_records = data.get("express") or []

    # 1. 快报实际值 → beat/miss（有预告）或符号定方向（无预告）
    express_events, covered = _express_evidence(express_records, forecast_records, symbol)

    # 2. 无对应快报的预告 → 预告方向证据
    forecast_events = _forecast_evidence(forecast_records, covered, symbol)

    # 3. 分析师盈利预测修正（revision）
    revision_events = _revision_evidence(_consensus_values(consensus), symbol, as_of_date or _consensus_date(consensus))

    return _dedup([*express_events, *forecast_events, *revision_events])
