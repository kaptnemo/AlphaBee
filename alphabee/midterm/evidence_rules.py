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
- **相关证据压缩（A1）**：同一报告期（period）至多一条数值证据（express 覆盖 forecast）；
  express 与 forecast 同口径按 ``ann_date`` 升序**取末条**（P0 设计 §3.2，修复取首条
  拿到被更正前旧预告的问题）；revision 的 1m/3m 窗口高度重叠（同一批分析师修正动量），
  同一预测年度只保留 |幅度| 最大的一条——避免同源相关证据重复累加 log-odds 虚增置信度。
- **预告修正 retraction（P0 设计 §3.2）**：同一报告期出现 ``forecast_update_flag`` 标记的
  末条预告时，生成 ``effect=neutral`` / ``confidence_delta=0`` / ``supersedes=[旧预告事件 id]``
  的 retraction 事件作废旧预告，修正后区间再由 beat/miss / 预告方向事件（flow，新 id）承接。
- **revision 日期口径（P0-1）**：revision 事件 date 用 ``consensus_as_of_date``
  （``ConsensusOutput.as_of_date``），缺失回落 ``as_of_date``（fail-open），使同一
  revision 数值跨帧产生同 id。

预告类型信息量分档与区间保守锚（E 生态设计 ``E_ECOSYSTEM_FIX_DESIGN.md`` §3/§4）：

- **类型分档（§3）**：``forecast_type``（adapter 已把 tushare ``type`` 映射为 canonical）
  按信息量分三档（:data:`_FORECAST_TYPE_TIER`）：``reversal``（扭亏/首亏——状态反转，
  A 股强制披露且盈利预期将从 0 重定价，信息熵最高）、``magnitude``（预增/预减/略增/略减
  ——幅度变化，常规信息量，即现行行为的设计对象）、``inertia``（续盈/续亏——方向与上年
  一致，已被上年财报定价，边际信息量最低）；缺失 / "不确定" / 未知取值 → ``unknown``
  （fail-open，按现行行为，不猜档位）。语义依据 = 设计 §3.3。
- **分档应用（§3.2）**：① ``_express_evidence`` 的 beat/miss 强度按档**升降一档**
  （reversal 升、inertia 降；STRONG 封顶、WEAK 降档后仍产事件）——升降后仍只在
  weak/medium/strong 三值域内，``confidence_delta`` 三值纪律不破；②
  ``_forecast_evidence`` 中 ``inertia`` ⇒ **不产事件**（取消而非降权：续盈/续亏确认的是
  市场已被上年财报定价的方向，"无意外即无证据"，事件本身几乎无新信息）；③
  ``_forecast_retraction_evidence`` **不受档位影响**（作废动作的信息量不因预告类型变化），
  其 ``supersedes`` 的旧事件 id 仍按同一保守锚口径重构以保证签名一致。
- **区间保守锚（§4.1）**：符号定方向不再用区间中点（:func:`_midpoint` 已移除），改由
  :func:`_conservative_direction` 定方向与幅度：``lo > 0`` ⇒ confirming，幅度 = |lo|
  （下限为正 ⇒ 最坏情形也增长）；``hi < 0`` ⇒ refuting，幅度 = |hi|（上限为负 ⇒ 最乐观
  情形也下滑）；跨零（``lo <= 0 <= hi``）⇒ 方向不确定，仅 ``reversal`` 且净利润绝对额
  下限 ``forecast_net_profit_min > 0`` 时以绝对额兜底给 confirming(weak)（扭亏同比基数
  为负，下限跨零属制度产物）；其余跨零 / 全缺失 ⇒ 不产事件（方向不确定不编造）。理由：
  A 股预告区间**普遍偏乐观**（上限给得宽裕、实际多贴近下限），中点把不确定性平均化并
  系统性高估预期；下限（confirming）/ 上限（refuting）是可得的最保守预期，且与贝叶斯
  "反证更有信息量"（``bayes._REFUTING_WEIGHT = 1.2``）的方向保守主义一致。事件
  ``value`` 记**保守锚值**（lo 或 hi，非中点）、``description`` 注明区间与"下限/上限
  保守锚"口径（自证不猜测）。
- **与 score 层口径差异披露（§4.2）**：``score_engine._forecast_revision_anchor``（P0-6
  预告锚，0.6 权重）比较相邻报告期的**区间中点差**，本模块证据流改用**保守锚**——两处
  口径**有意不同**：score 层度量"预告水平自身的方向变化"（中点差抵消区间形态差异），
  evidence 层度量"单条预告对 thesis 的保守方向证据"（宁可低估不可高估）。PR-A 不动
  score 层（设计 §10 非目标）；消费/对比时须知同一报告期两处数值不可直接比对。
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

# P0 设计 §9.1：revision 证据的 coverage 护栏（单份研报调价不可信）。
_MIN_CONSENSUS_COVERAGE = 3

# 数值类证据统一受控 kind（§3：fundamental / expectation / trend / crowding / thesis / price）
_KIND = "expectation"

# 预告类型信息量分档（E 生态设计 §3.1；tushare ``forecast.type`` 取值域，adapter 已映射为
# canonical ``forecast_type``）：
# - reversal：状态反转（上年亏损→盈利 / 盈利→亏损），强制披露且盈利预期将重定价，信息熵最高；
# - magnitude：幅度变化（预增/预减/略增/略减），常规信息量，现行 beat/miss 表的设计对象；
# - inertia：惯性持续（续盈/续亏，方向与上年一致），已被上年财报定价，边际信息量最低。
# 缺失 / "不确定" / 未知取值 → :data:`_DEFAULT_TIER`（fail-open，按现行行为，不猜档位）。
_FORECAST_TYPE_TIER: dict[str, str] = {
    "扭亏": "reversal",
    "首亏": "reversal",
    "预增": "magnitude",
    "预减": "magnitude",
    "略增": "magnitude",
    "略减": "magnitude",
    "续盈": "inertia",
    "续亏": "inertia",
}
_DEFAULT_TIER = "unknown"

# 强度三值升序（升降档在两个方向上的封顶/兜底依据，保证 confidence_delta 三值纪律）
_STRENGTH_ORDER: tuple[Strength, ...] = (Strength.WEAK, Strength.MEDIUM, Strength.STRONG)

# revision 字段按预测年度分组（A1 相关证据压缩）：1m/3m 窗口高度重叠
# （同一批分析师修正动量），同一预测年度只保留 |幅度| 最大的一条；fy1 / fy2
# 分属不同预测年度，各自保留一条。顺序即组内 tie-break 优先级。
_REVISION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("eps_fy1_revision_1m", "eps_fy1_revision_3m"),
    ("eps_fy2_revision_1m",),
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


def _to_int(value: Any) -> int | None:
    """同 :func:`_to_float`，但产出 ``int | None``（用于 coverage_count 等计数）。"""
    v = _to_float(value)
    return int(v) if v is not None else None


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


def _is_update_flag(value: Any) -> bool:
    """预告修正标志判定（P0 设计 §3.2）：``update_flag`` 非空且非 0/false → True。

    tushare ``update_flag`` 为字符串标记（如 "1" = 修正）；缺失 / "0" / "None" /
    "nan" / "false" / "no" 一律视为「无修正」（fail-open：无修正不产 retraction）。
    """
    if value is None:
        return False
    text = str(value).strip().lower()
    return text not in ("", "0", "none", "null", "nan", "false", "no", "-")


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


def _forecast_tier(rec: dict[str, Any]) -> str:
    """预告记录的类型档位（E 生态设计 §3.1）：读 canonical ``forecast_type``。

    缺失 / 空串 / "不确定" / 未知取值 / 非 dict 记录 → :data:`_DEFAULT_TIER`
    （fail-open：宁可当作常规信息量，不猜档位、不因新取值抛错）。
    """
    if not isinstance(rec, dict):
        return _DEFAULT_TIER
    raw = rec.get("forecast_type")
    if raw is None or isinstance(raw, bool):
        return _DEFAULT_TIER
    text = str(raw).strip()
    return _FORECAST_TYPE_TIER.get(text, _DEFAULT_TIER)


def _shift_strength(strength: Strength, tier: str) -> Strength:
    """按预告类型档位升降一档（E 生态设计 §3.2 ①，只动档位、不动数值表）。

    ``reversal`` 升一档（STRONG 封顶）、``inertia`` 降一档（WEAK 降档后仍产事件）、
    ``magnitude`` / ``unknown`` 不动；升降结果恒落在 weak/medium/strong 三值域，
    ``confidence_delta`` 三值纪律不破。
    """
    if strength not in _STRENGTH_ORDER:
        return strength
    idx = _STRENGTH_ORDER.index(strength)
    if tier == "reversal":
        return _STRENGTH_ORDER[min(idx + 1, len(_STRENGTH_ORDER) - 1)]
    if tier == "inertia":
        return _STRENGTH_ORDER[max(idx - 1, 0)]
    return strength


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
        subject=subject,
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
    """按 ``period`` 建索引，**按 ``ann_date`` 升序取末条**（P0 设计 §3.2）。

    修复既有"取首条可能拿到被更正前的旧预告"问题：express 与 forecast 同口径，
    同报告期多条记录时以公告日最晚的一条为准（末条 = 修正后口径）。
    ``ann_date`` 缺失时回落 ``period`` 参与比较（同值下后出现的记录胜出）。
    """
    out: dict[str, dict[str, Any]] = {}
    for rec in records or []:
        p = str(rec.get("period") or "").strip()
        if not p:
            continue
        current = out.get(p)
        if current is None:
            out[p] = rec
            continue
        cur_key = _norm_date(current.get("ann_date")) or _norm_date(current.get("period"))
        new_key = _norm_date(rec.get("ann_date")) or _norm_date(rec.get("period"))
        if new_key >= cur_key:
            out[p] = rec
    return out


def _is_reversal_cross_zero(
    lo: float | None,
    hi: float | None,
    tier: str,
    net_profit_min: float | None,
) -> bool:
    """扭亏跨零的制度性分支判定（E 生态设计 §4.1）。

    同比区间跨零（``lo <= 0 <= hi``）通常意味着方向不确定；但 ``reversal``（扭亏/首亏）
    的同比基数本身为负，下限跨越 0 属制度产物——此时若净利润绝对额下限
    ``forecast_net_profit_min > 0``（下限已为正利润），允许以绝对额兜底给
    confirming(weak)，避免把"同比百分比跨零"误判成"方向未知"。
    """
    if tier != "reversal" or lo is None or hi is None:
        return False
    if not (lo <= 0.0 <= hi):
        return False
    return net_profit_min is not None and net_profit_min > 0.0


def _conservative_direction(
    lo: float | None,
    hi: float | None,
    tier: str,
    net_profit_min: float | None = None,
) -> tuple[EffectOnThesis | None, float | None]:
    """符号定方向的保守锚（E 生态设计 §4.1，替代区间中点口径）。

    - ``lo > 0`` ⇒ confirming，幅度 = |lo|（下限为正 ⇒ 最坏情形也增长）；
    - ``hi < 0`` ⇒ refuting，幅度 = |hi|（上限为负 ⇒ 最乐观情形也下滑）；
    - 跨零（``lo <= 0 <= hi``）⇒ 方向不确定：
      ``tier == "reversal"`` 且绝对额下限 ``forecast_net_profit_min > 0`` ⇒
      confirming + 绝对额（净利润下限，非百分比；扭亏跨零是制度产物，以绝对额兜底），
      其余 ⇒ ``(None, None)``（不产事件——方向不确定不编造）；
    - 单边缺失且符号不能定方向 / 全缺失 ⇒ ``(None, None)``（同一 fail-closed 口径）。

    返回的 ``value`` 即事件记的**保守锚值**（下限或上限，不是中点）。
    """
    if lo is not None and lo > 0.0:
        return EffectOnThesis.CONFIRMING, lo
    if hi is not None and hi < 0.0:
        return EffectOnThesis.REFUTING, abs(hi)
    if _is_reversal_cross_zero(lo, hi, tier, net_profit_min):
        return EffectOnThesis.CONFIRMING, net_profit_min
    return None, None


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
    if v is None:
        return "业绩预告净利润同比区间缺失"
    return f"业绩预告净利润同比 {v:+.2f}%"


def _anchor_description(
    lo: float | None,
    hi: float | None,
    effect: EffectOnThesis,
    anchor: float,
) -> str:
    """预告方向事件 description：区间 + 保守锚口径（自证不猜测，E 生态设计 §4.1）。"""
    base = _forecast_description(lo, hi)
    if effect is EffectOnThesis.CONFIRMING:
        raw = lo if lo is not None else anchor
        return f"{base}，按下限保守锚（{raw:+.2f}%）计"
    raw = hi if hi is not None else -anchor
    return f"{base}，按上限保守锚（{raw:+.2f}%）计"


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

    # A1 相关证据压缩：同一报告期多条快报记录 → 按 ann_date 升序取末条（与 forecast 侧
    # ``_index_by_period`` 同口径），保证每个 period 至多一条 beat/miss 证据。
    for rec in _index_by_period(express_records).values():
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
        # 档位取自同报告期预告记录的类型（快报本身不带 forecast_type；无预告 → unknown）
        tier = _forecast_tier(fc) if fc is not None else _DEFAULT_TIER

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
                        # E 生态设计 §3.2 ①：reversal 升一档 / inertia 降一档，三值域内升降
                        strength=_shift_strength(_calibrate_beat(dev), tier),
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
    """业绩预告（无对应快报）→ 预告方向证据（保守锚定方向，E 生态设计 §3.2 ②/§4）。

    - ``inertia``（续盈/续亏）⇒ **不产事件**（取消而非降权：方向已被上年财报定价）；
    - ``reversal`` / ``magnitude`` / ``unknown`` ⇒ 方向与幅度走
      :func:`_conservative_direction`（下限/上限保守锚，不再用区间中点）；
    - 跨零且 ``reversal`` 且净利润绝对额下限 > 0 ⇒ confirming(weak) 绝对额兜底；
    - 其余跨零 / 全缺失 ⇒ 不产事件（方向不确定不编造）。
    """
    events: list[EvidenceEvent] = []
    # A1 相关证据压缩：同一报告期多条预告记录 → 按 ann_date 升序取末条，保证每个 period
    # 至多一条预告证据（重复快照不重复累加 log-odds）。
    for rec in _index_by_period(forecast_records).values():
        period = str(rec.get("period") or "").strip()
        if period in covered_periods:
            continue  # 已有快报实际值 → beat/miss 已覆盖，同主题不重复计
        tier = _forecast_tier(rec)
        if tier == "inertia":
            continue  # §3.2 ②：续盈/续亏方向已被上年财报定价 → 取消（不是降权）
        lo = _to_float(rec.get("profit_forecast_min_change"))
        hi = _to_float(rec.get("profit_forecast_max_change"))
        net_profit_min = _to_float(rec.get("forecast_net_profit_min"))
        effect, anchor = _conservative_direction(lo, hi, tier, net_profit_min)
        if effect is None or anchor is None:
            continue  # 跨零/缺失：方向不确定 → 不产事件（防幻觉）
        reversal_fallback = _is_reversal_cross_zero(lo, hi, tier, net_profit_min)
        if reversal_fallback:
            # 绝对额只定方向、不定幅度（净利润绝对额非百分比，不能用 beat 表标定）
            strength = Strength.WEAK
            description = f"{_forecast_description(lo, hi)}，扭亏跨零：净利润下限为正，按绝对额兜底定方向（weak）"
        else:
            strength = _calibrate_beat(anchor)
            description = _anchor_description(lo, hi, effect, anchor)
        date = _norm_date(rec.get("ann_date")) or _norm_date(rec.get("period"))
        events.append(
            _make_event(
                date=date,
                subject=f"{symbol}:forecast:{period}",
                value=anchor,
                description=description,
                effect=effect,
                strength=strength,
                source_refs=[_forecast_source(symbol)],
            )
        )
    return events


def _forecast_retraction_evidence(
    forecast_records: list[dict[str, Any]],
    symbol: str,
) -> list[EvidenceEvent]:
    """预告修正 → retraction 事件（P0 设计 §3.2，作废旧预告、不携带方向贡献）。

    同一报告期出现 ``forecast_update_flag`` 标记的**末条**预告（按 ``ann_date`` 升序）
    时：以末条前一条预告（被修正前的版本）重构旧预告事件 id，生成 ``effect=neutral``、
    ``confidence_delta=0``、``supersedes=[旧预告事件 id]`` 的 retraction 事件。
    修正后区间本身由 ``_express_evidence`` / ``_forecast_evidence`` 走既有 beat/miss /
    预告方向事件（flow，新 id）——此处只作废，不重复计方向贡献。

    E 生态设计 §3.2 ③：retraction **不受预告类型档位影响**（作废动作的信息量不因
    类型变化，续盈/续亏类修正预告照常作废）；旧事件 id 仍按与 ``_forecast_evidence``
    相同的保守锚口径重构（§4.1），保证 supersedes 指向真实旧事件。旧预告在保守锚下
    不产方向事件（跨零 / 全缺失）⇒ 无对象可作废，本处 fail-closed 不产 retraction。
    """
    by_period: dict[str, list[dict[str, Any]]] = {}
    for rec in forecast_records or []:
        period = str(rec.get("period") or "").strip()
        if period:
            by_period.setdefault(period, []).append(rec)

    events: list[EvidenceEvent] = []
    for period, records in by_period.items():
        if len(records) < 2:
            continue
        ordered = sorted(
            records,
            key=lambda r: _norm_date(r.get("ann_date")) or _norm_date(r.get("period")),
        )
        last = ordered[-1]
        if not _is_update_flag(last.get("forecast_update_flag")):
            continue
        previous = ordered[-2]
        old_lo = _to_float(previous.get("profit_forecast_min_change"))
        old_hi = _to_float(previous.get("profit_forecast_max_change"))
        old_net_profit_min = _to_float(previous.get("forecast_net_profit_min"))
        # 与 _forecast_evidence 同口径重构旧预告事件签名（保守锚值，不是中点）
        _, old_anchor = _conservative_direction(old_lo, old_hi, _forecast_tier(previous), old_net_profit_min)
        if old_anchor is None:
            continue  # 旧预告本身不产方向事件 ⇒ 无可作废对象
        old_date = _norm_date(previous.get("ann_date")) or _norm_date(previous.get("period"))
        old_id = _event_id(old_date, _KIND, f"{symbol}:forecast:{period}", old_anchor)
        retract_date = _norm_date(last.get("ann_date")) or _norm_date(last.get("period"))
        events.append(
            EvidenceEvent(
                id=_event_id(retract_date, _KIND, f"{symbol}:retraction:{period}", None),
                date=retract_date,
                kind=_KIND,
                subject=f"{symbol}:retraction:{period}",
                description=f"预告修正：{period} 业绩预告作废（旧预告 {_forecast_description(old_lo, old_hi)}）",
                effect_on_thesis=EffectOnThesis.NEUTRAL,
                confidence_delta=0.0,
                source_refs=[_forecast_source(symbol)],
                supersedes=[old_id],
            )
        )
    return events


def _revision_evidence(
    values: dict[str, Any],
    symbol: str,
    as_of_date: str,
) -> list[EvidenceEvent]:
    """分析师盈利预测修正 → 方向证据（方向=符号，幅度离散标定 §6）。

    P0 设计 §9.1：coverage 护栏——``coverage_count`` 缺失或 <
    :data:`_MIN_CONSENSUS_COVERAGE`（3）⇒ ``[]``（单份研报调价不产 revision 证据）。

    A1 相关证据压缩：按预测年度分组，组内只保留 |幅度| 最大的一条
    （最强证据）——1m/3m 窗口重叠，同源修正动量不重复累加 log-odds；
    同组方向冲突时按幅度决定（信息量最大者胜），不按字段数多数表决。
    """
    coverage = _to_int(values.get("coverage_count"))
    if coverage is None or coverage < _MIN_CONSENSUS_COVERAGE:
        return []  # 覆盖不足 ⇒ 不产 revision 证据（防单份研报调价即 strong）

    # P0-1：事件 date 用 consensus_as_of_date（跨帧同 id），缺失回落 as_of_date
    # （fail-open 不抛）——同一 revision 数值在跨帧产生同 id，差集不再误报新证据。
    event_date = str(values.get("consensus_as_of_date") or "").strip() or as_of_date

    events: list[EvidenceEvent] = []
    for fields in _REVISION_GROUPS:
        best_field: str | None = None
        best_value: float | None = None
        for field in fields:
            v = _to_float(values.get(field))
            if v is None:
                continue
            if best_value is None or abs(v) > abs(best_value):
                best_field, best_value = field, v
        if best_field is None or best_value is None:
            continue  # 全缺失：不产事件（防幻觉）
        effect = _effect_from_sign(best_value)
        if effect is EffectOnThesis.NEUTRAL:
            continue
        events.append(
            _make_event(
                date=event_date,
                subject=f"{symbol}:revision:{best_field}",
                value=best_value,
                description=_revision_description(best_field, best_value),
                effect=effect,
                strength=_calibrate_revision(best_value),
                source_refs=[_revision_source(symbol)],
            )
        )
    return events


def _consensus_values(consensus: Any) -> dict[str, Any]:
    """归一化 consensus 输入：接受 ``ConsensusOutput``（含 ``.values``）或 plain dict。

    注意：必须优先判 ``dict``——dict 自带 ``.values`` 方法，若先判 ``hasattr(values)``
    会把 bound method 误当数据。

    P0 设计 §2.1：返回 dict 追加 ``consensus_as_of_date``（源自 ``ConsensusOutput.as_of_date``，
    ``collectors/consensus/engine.py`` 已有）；``coverage_count`` 随 ``values`` 透传
    （``ConsensusOutput.values`` 已含该 canonical 字段）。
    """
    if consensus is None:
        return {}
    if isinstance(consensus, dict):
        inner = consensus.get("values")
        base = inner if isinstance(inner, dict) else consensus
    elif hasattr(consensus, "values"):
        base = consensus.values or {}
    else:
        base = {}
    out = dict(base)
    as_of = _consensus_date(consensus)
    if as_of:
        out["consensus_as_of_date"] = as_of
    return out


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

    # 2. 无对应快报的预告 → 预告方向证据（同报告期取 ann_date 末条，P0 §3.2）
    forecast_events = _forecast_evidence(forecast_records, covered, symbol)

    # 3. 预告修正 → retraction 事件（作废旧预告，P0 §3.2；neutral，不携带方向贡献）
    retraction_events = _forecast_retraction_evidence(forecast_records, symbol)

    # 4. 分析师盈利预测修正（revision；事件 date 用 consensus_as_of_date，P0-1）
    revision_events = _revision_evidence(_consensus_values(consensus), symbol, as_of_date or _consensus_date(consensus))

    return _dedup([*express_events, *forecast_events, *retraction_events, *revision_events])
