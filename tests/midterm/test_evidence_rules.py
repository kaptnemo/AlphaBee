"""数值类 EvidenceEvent 规则生成器（E1）单测。

覆盖设计 ``MIDTERM_EVIDENCE_EXTRACTION.md`` §6/§7/§8 的数值类规则：

- beat/miss（express 实际 vs forecast 预告区间）→ 方向 + 离散标定（§6）；
- revision 幅度（方向=符号）→ 离散标定（§6）；
- 符号定方向退化（§8，thesis 为空 / 无预告基线）；
- 事件签名去重 id=hash(date+kind+主体+数值)，多来源合并 source_refs（§7）；
- 防幻觉：缺失/冲突数值置 None，不产事件、不静默回退（§5）；
- 预告类型信息量分档与区间保守锚（E 生态设计 §3/§4，PR-A）：类型三档升降、
  inertia 取消、retraction 不受档位影响、下限/上限保守锚与跨零三档。
"""

import pytest

from alphabee.midterm.bayes import update_confidence, update_confidence_delta
from alphabee.midterm.evidence_rules import (
    _DEFAULT_TIER,
    _FORECAST_TYPE_TIER,
    _calibrate_beat,
    _calibrate_revision,
    _conservative_direction,
    _dedup,
    _event_id,
    _forecast_tier,
    _norm_date,
    _shift_strength,
    _to_float,
    build_numeric_evidence,
)
from alphabee.midterm.models import EvidenceEvent, Strength

DISCRETE_DELTAS = {0.1, 0.3, 0.5}  # §6：weak/medium/strong，禁连续值


def _exp(forecast=None, express=None):
    return {"forecast": forecast or [], "express": express or []}


def _fc(period="20231231", ann="20240130", lo=10.0, hi=15.0):
    return {
        "period": period,
        "ann_date": ann,
        "profit_forecast_min_change": lo,
        "profit_forecast_max_change": hi,
    }


def _ex(period="20231231", ann="20240320", np_yoy=18.0):
    return {"period": period, "ann_date": ann, "express_net_profit_yoy": np_yoy}


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────
def test_to_float_missing_and_invalid_are_none():
    assert _to_float(None) is None
    assert _to_float("") is None
    assert _to_float("--") is None
    assert _to_float("nan") is None
    assert _to_float(float("nan")) is None
    assert _to_float(12.5) == 12.5
    assert _to_float("3.2") == 3.2


def test_norm_date_yyyymmdd_and_iso():
    assert _norm_date("20240320") == "2024-03-20"
    assert _norm_date("2024-03-20") == "2024-03-20"
    assert _norm_date("") == ""
    assert _norm_date(None) == ""


def test_event_id_deterministic_and_value_sensitive():
    a = _event_id("2024-03-20", "expectation", "600519:beat:20231231", 18.0)
    b = _event_id("2024-03-20", "expectation", "600519:beat:20231231", 18.0)
    c = _event_id("2024-03-20", "expectation", "600519:beat:20231231", 19.0)
    assert a == b  # 同签名 → 同 id（去重键稳定）
    assert a != c  # 数值不同 → 不同 id


# ─────────────────────────────────────────────────────────────────────────────
# 离散标定（§6，禁连续值）
# ─────────────────────────────────────────────────────────────────────────────
def test_calibrate_beat_boundaries():
    # <5 weak / 5–15 medium / >15 strong
    assert _calibrate_beat(3.0) is Strength.WEAK
    assert _calibrate_beat(4.99) is Strength.WEAK
    assert _calibrate_beat(5.0) is Strength.MEDIUM
    assert _calibrate_beat(15.0) is Strength.MEDIUM
    assert _calibrate_beat(15.01) is Strength.STRONG
    assert _calibrate_beat(-20.0) is Strength.STRONG  # 幅度取绝对值


def test_calibrate_revision_boundaries():
    # |Δ|<3 weak / 3–10 medium / >10 strong
    assert _calibrate_revision(2.0) is Strength.WEAK
    assert _calibrate_revision(2.99) is Strength.WEAK
    assert _calibrate_revision(3.0) is Strength.MEDIUM
    assert _calibrate_revision(10.0) is Strength.MEDIUM
    assert _calibrate_revision(10.01) is Strength.STRONG
    assert _calibrate_revision(-12.0) is Strength.STRONG


# ─────────────────────────────────────────────────────────────────────────────
# beat/miss（express vs forecast）
# ─────────────────────────────────────────────────────────────────────────────
def test_beat_weak():
    # actual=18 vs [10,15] → 超上限 3pp → weak 0.1 confirming
    events = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=18.0)]), symbol="600519")
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"
    assert e.confidence_delta == 0.1
    assert "18.00" in e.description and "15.00" in e.description
    assert e.source_refs == ["tushare:express:600519", "tushare:forecast:600519"]


def test_beat_medium_and_strong():
    medium = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=22.0)]))[0]
    assert medium.effect_on_thesis == "confirming"
    assert medium.confidence_delta == 0.3  # 超上限 7pp → medium

    strong = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=35.0)]))[0]
    assert strong.effect_on_thesis == "confirming"
    assert strong.confidence_delta == 0.5  # 超上限 20pp → strong


def test_miss_refuting():
    # actual=5 vs [10,15] → 低于下限 5pp → medium 0.3 refuting
    events = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=5.0)]))
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "refuting"
    assert e.confidence_delta == 0.3


def test_inline_is_neutral_no_event():
    # actual=12 落在 [10,15] → 无意外 → 不产事件
    events = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=12.0)]))
    assert events == []


# ─────────────────────────────────────────────────────────────────────────────
# 符号定方向退化（§8）
# ─────────────────────────────────────────────────────────────────────────────
def test_express_without_forecast_sign_direction():
    # 无预告 → 实际同比按符号定方向，幅度复用 beat 表
    events = build_numeric_evidence(_exp(express=[_ex(np_yoy=-8.0)]), symbol="000001")
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "refuting"
    assert e.confidence_delta == 0.3  # |−8| ∈ [5,15] → medium
    assert e.source_refs == ["tushare:express:000001"]


def test_forecast_without_express_sign_direction():
    # 预告 min=20/max=30 → 下限保守锚 +20（PR-A §4）→ confirming，|20|>15 → strong
    events = build_numeric_evidence(_exp(forecast=[_fc(lo=20.0, hi=30.0)]))
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"
    assert e.confidence_delta == 0.5
    assert "[+20.00%, +30.00%]" in e.description
    assert "下限保守锚" in e.description


# ─────────────────────────────────────────────────────────────────────────────
# revision（方向=符号，幅度离散标定）
# ─────────────────────────────────────────────────────────────────────────────
def test_revision_weak_medium_strong_and_direction():
    cons = {"eps_fy1_revision_1m": 2.0, "coverage_count": 3}
    e = build_numeric_evidence(None, cons, as_of_date="2024-03-20")[0]
    assert e.effect_on_thesis == "confirming"
    assert e.confidence_delta == 0.1  # |2| < 3 → weak
    assert e.date == "2024-03-20"

    e = build_numeric_evidence(None, {"eps_fy1_revision_1m": -5.0, "coverage_count": 3})[0]
    assert e.effect_on_thesis == "refuting"  # 方向=符号
    assert e.confidence_delta == 0.3  # 3 ≤ |−5| ≤ 10 → medium

    e = build_numeric_evidence(None, {"eps_fy1_revision_1m": 12.0, "coverage_count": 3})[0]
    assert e.effect_on_thesis == "confirming"
    assert e.confidence_delta == 0.5  # >10 → strong


def test_revision_zero_and_missing_no_event():
    assert build_numeric_evidence(None, {"eps_fy1_revision_1m": 0.0, "coverage_count": 3}) == []
    assert build_numeric_evidence(None, {"eps_fy1_revision_1m": None, "coverage_count": 3}) == []
    assert build_numeric_evidence(None, {}) == []


def test_revision_compressed_to_strongest_per_fiscal_year():
    # A1 相关证据压缩：fy1 的 1m/3m 窗口重叠 → 只保留 |幅度| 最大的一条（8.0）；
    # fy2 属另一预测年度，独立保留一条（-2.0）。
    cons = {
        "eps_fy1_revision_1m": 4.0,
        "eps_fy1_revision_3m": 8.0,
        "eps_fy2_revision_1m": -2.0,
        "coverage_count": 3,
    }
    events = build_numeric_evidence(None, cons)
    assert len(events) == 2
    fy1 = next(e for e in events if "FY1" in e.description)
    fy2 = next(e for e in events if "FY2" in e.description)
    assert "近3月" in fy1.description  # fy1 组内最强来自 3m 窗口
    assert fy1.effect_on_thesis == "confirming"
    assert fy1.confidence_delta == 0.3  # |8| → medium
    assert fy2.effect_on_thesis == "refuting"
    assert fy2.confidence_delta == 0.1  # |−2| → weak
    assert {e.confidence_delta for e in events} <= DISCRETE_DELTAS


def test_revision_opposite_signs_keep_strongest():
    # A1：同组方向冲突（1m=+4 / 3m=−9）→ 按 |幅度| 决定（−9 胜出），不按字段数多数表决
    events = build_numeric_evidence(
        None, {"eps_fy1_revision_1m": 4.0, "eps_fy1_revision_3m": -9.0, "coverage_count": 3}
    )
    assert len(events) == 1
    assert events[0].effect_on_thesis == "refuting"
    assert events[0].confidence_delta == 0.3  # |−9| → medium


def test_duplicate_express_same_period_uses_latest_ann_date():
    # P0 §3.2：同一报告期多条快报记录 → 按 ann_date 升序取末条（修复取首条拿旧口径）
    events = build_numeric_evidence(_exp(express=[_ex(np_yoy=18.0), _ex(ann="20240401", np_yoy=25.0)]))
    assert len(events) == 1
    assert events[0].date == "2024-04-01"  # 末条记录（_norm_date → ISO）
    assert events[0].confidence_delta == 0.5  # |25| > 15 → strong


def test_duplicate_forecast_same_period_uses_latest_ann_date():
    # P0 §3.2：同一报告期多条预告记录 → 按 ann_date 升序取末条
    events = build_numeric_evidence(_exp(forecast=[_fc(), _fc(ann="20240401", lo=20.0, hi=25.0)]))
    assert len(events) == 1
    assert events[0].date == "2024-04-01"  # 末条记录（_norm_date → ISO）
    assert events[0].confidence_delta == 0.5  # 末条下限保守锚 20 → strong


# ─────────────────────────────────────────────────────────────────────────────
# 防幻觉（§5）：缺失 / 冲突不编造
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_actual_no_event():
    # 快报无实际同比、且无预告 → 不产事件（防幻觉：不编造）
    events = build_numeric_evidence(_exp(express=[{"period": "20231231", "express_net_profit_yoy": None}]))
    assert events == []


def test_missing_forecast_range_no_guidance_event():
    events = build_numeric_evidence(
        _exp(forecast=[{"period": "20231231", "profit_forecast_min_change": None, "profit_forecast_max_change": None}])
    )
    assert events == []


def test_conflict_range_falls_back_to_sign_direction():
    # lo > hi（冲突）→ 预告区间无效，降级为符号定方向（只认实际值，§5 冲突降级）
    events = build_numeric_evidence(_exp(forecast=[_fc(lo=20.0, hi=10.0)], express=[_ex(np_yoy=25.0)]))
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"  # 25>0 → confirming
    assert e.confidence_delta == 0.5  # |25| > 15 → strong
    # 冲突时预告不作为来源（只认结构化实际值）
    assert e.source_refs == ["tushare:express"]


# ─────────────────────────────────────────────────────────────────────────────
# 去重（§7）：多来源合并、同主题只算一次
# ─────────────────────────────────────────────────────────────────────────────
def test_dedup_merges_source_refs_same_id():
    a = EvidenceEvent(
        id="same",
        date="2024-03-20",
        kind="expectation",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.1,
        source_refs=["src-a"],
    )
    b = EvidenceEvent(
        id="same",
        date="2024-03-20",
        kind="expectation",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.1,
        source_refs=["src-b"],
    )
    merged = _dedup([a, b])
    assert len(merged) == 1
    assert merged[0].source_refs == ["src-a", "src-b"]


def test_beat_event_merges_two_sources():
    # beat/miss 事件天然合并 forecast + express 两个结构化来源（§7 多来源合并）
    events = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=18.0)]), symbol="600519")
    assert len(events) == 1
    assert events[0].source_refs == ["tushare:express:600519", "tushare:forecast:600519"]


def test_same_period_counted_once():
    # 同报告期同时有预告+快报 → 只产一条 beat/miss 事件（不重复计预告/快报）
    events = build_numeric_evidence(_exp(forecast=[_fc()], express=[_ex(np_yoy=18.0)]))
    assert len(events) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 离散值不变量（禁连续值）
# ─────────────────────────────────────────────────────────────────────────────
def test_all_emitted_deltas_are_discrete():
    exp = _exp(
        forecast=[_fc(lo=10.0, hi=15.0), _fc(period="20230930", lo=-5.0, hi=5.0)],
        express=[_ex(np_yoy=35.0), _ex(period="20230930", np_yoy=-20.0)],
    )
    cons = {"eps_fy1_revision_1m": 4.0, "eps_fy1_revision_3m": -9.0, "coverage_count": 3}
    events = build_numeric_evidence(exp, cons)
    assert events
    for e in events:
        assert e.effect_on_thesis in ("confirming", "refuting")  # neutral 不产事件
        assert e.confidence_delta in DISCRETE_DELTAS


# ─────────────────────────────────────────────────────────────────────────────
# P0-1：revision 跨帧幂等（§2.3 四条）
# ─────────────────────────────────────────────────────────────────────────────


def _cons(values: dict | None = None, as_of_date: str = "2024-03-20") -> dict:
    return {"as_of_date": as_of_date, "values": dict(values or {})}


def test_revision_same_value_same_id_across_frames():
    """同一 revision 数值、不同 as_of_date ⇒ 事件 id 逐字相等（P0-1 根因①消除）。"""
    cons = _cons({"eps_fy1_revision_1m": 4.0, "coverage_count": 3}, as_of_date="2024-03-20")
    e1 = build_numeric_evidence(None, cons, symbol="600519", as_of_date="2024-03-21")[0]
    e2 = build_numeric_evidence(None, cons, symbol="600519", as_of_date="2024-03-22")[0]
    assert e1.id == e2.id
    assert e1.date == "2024-03-20"  # 用 consensus_as_of_date，非帧日期


def test_revision_date_falls_back_to_as_of_date_when_consensus_date_missing():
    """consensus_as_of_date 缺失 ⇒ 回落 as_of_date（fail-open，不抛）。"""
    cons = {"values": {"eps_fy1_revision_1m": 4.0, "coverage_count": 3}}  # 无 as_of_date
    e = build_numeric_evidence(None, cons, symbol="600519", as_of_date="2024-03-22")[0]
    assert e.date == "2024-03-22"


def test_revision_level_replace_net_effect():
    """同 subject 新值 ⇒ 净 log-odds = logodds(新) − logodds(旧)（§2.3 主钉）。"""
    old = EvidenceEvent(
        id="rev-old",
        date="2024-03-20",
        kind="expectation",
        subject="600519:revision:eps_fy1_revision_1m",
        description="",
        effect_on_thesis="confirming",
        confidence_delta=0.1,
    )
    new = EvidenceEvent(
        id="rev-new",
        date="2024-03-20",
        kind="expectation",
        subject="600519:revision:eps_fy1_revision_1m",
        description="",
        effect_on_thesis="confirming",
        confidence_delta=0.5,
    )
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    assert post == pytest.approx(update_confidence([new], prior=0.5))


def test_revision_same_value_repeat_frame_zero_change():
    """同值重复帧 ⇒ 净变化 0、后验不动（P0-1 主钉）。"""
    cons = _cons({"eps_fy1_revision_1m": 4.0, "coverage_count": 3}, as_of_date="2024-03-20")
    events = build_numeric_evidence(None, cons, symbol="600519")
    prev_post = update_confidence(events, prior=0.5)
    post = update_confidence_delta(events, prior=prev_post, prev_events=events)
    assert post == pytest.approx(prev_post)


# ─────────────────────────────────────────────────────────────────────────────
# P0-2：update_flag 作废 / 末条口径 / 回归（§3.5）
# ─────────────────────────────────────────────────────────────────────────────


def _fc_update(period="20231231", ann="20240220", lo=20.0, hi=30.0, flag="1"):
    return {
        "period": period,
        "ann_date": ann,
        "profit_forecast_min_change": lo,
        "profit_forecast_max_change": hi,
        "forecast_update_flag": flag,
    }


def _forecast_event_id(record: dict, symbol: str) -> str:
    period = record["period"]
    lo = _to_float(record.get("profit_forecast_min_change"))
    hi = _to_float(record.get("profit_forecast_max_change"))
    net_min = _to_float(record.get("forecast_net_profit_min"))
    # PR-A §4：预告方向事件的 value 是保守锚（不是区间中点）
    _, anchor = _conservative_direction(lo, hi, _forecast_tier(record), net_min)
    date = _norm_date(record.get("ann_date")) or _norm_date(record.get("period"))
    return _event_id(date, "expectation", f"{symbol}:forecast:{period}", anchor)


def test_update_flag_produces_retraction_superseding_old_forecast():
    """update_flag 末条预告 ⇒ retraction（neutral/Δ=0/supersedes=旧预告 id）+ 新 flow 事件。"""
    old = _fc(ann="20240130", lo=10.0, hi=15.0)  # 旧预告（无 update_flag）
    corrected = _fc_update(ann="20240220", lo=20.0, hi=30.0)  # 修正版
    events = build_numeric_evidence(_exp(forecast=[old, corrected]), symbol="600519")

    retractions = [e for e in events if e.effect_on_thesis == "neutral" and e.supersedes]
    assert len(retractions) == 1
    ret = retractions[0]
    assert ret.supersedes == [_forecast_event_id(old, "600519")]
    assert ret.confidence_delta == 0.0  # retraction 不带方向贡献
    assert "预告修正" in ret.description

    # 修正后区间重新生成 beat/miss/预告方向事件（flow，新 id，非 retraction）
    flows = [e for e in events if e.effect_on_thesis in ("confirming", "refuting")]
    assert flows and all(e.id != _forecast_event_id(old, "600519") for e in flows)
    assert {e.confidence_delta for e in events if e.effect_on_thesis != "neutral"} <= DISCRETE_DELTAS


def test_same_period_forecast_picks_latest_ann_date_regardless_of_order():
    """末条口径钉：记录乱序（先修正版后旧版）仍按 ann_date 升序取末条。"""
    corrected = _fc_update(ann="20240220", lo=20.0, hi=30.0)
    old = _fc(ann="20240130", lo=10.0, hi=15.0)
    events = build_numeric_evidence(_exp(forecast=[corrected, old]), symbol="600519")
    flows = [e for e in events if e.effect_on_thesis in ("confirming", "refuting")]
    assert len(flows) == 1
    assert flows[0].date == "2024-02-20"
    assert "[+20.00%, +30.00%]" in flows[0].description


def test_no_update_flag_preserves_legacy_single_forecast_event():
    """无 update_flag 回归钉：存量数据路径行为不变（只有 flow，无 retraction）。"""
    events = build_numeric_evidence(_exp(forecast=[_fc(ann="20240130", lo=10.0, hi=15.0)]), symbol="600519")
    assert [e.effect_on_thesis for e in events] == ["confirming"]
    assert all(not e.supersedes for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# P0-6 §9.1：revision coverage 护栏
# ─────────────────────────────────────────────────────────────────────────────


def test_revision_coverage_below_threshold_returns_empty():
    cons = _cons({"eps_fy1_revision_1m": 4.0, "coverage_count": 2}, as_of_date="2024-03-20")
    assert build_numeric_evidence(None, cons, symbol="600519") == []


def test_revision_coverage_missing_returns_empty():
    cons = {"values": {"eps_fy1_revision_1m": 4.0}}  # 无 coverage_count
    assert build_numeric_evidence(None, cons, symbol="600519") == []


def test_revision_coverage_at_threshold_regression():
    cons = _cons({"eps_fy1_revision_1m": 4.0, "coverage_count": 3}, as_of_date="2024-03-20")
    events = build_numeric_evidence(None, cons, symbol="600519")
    assert len(events) == 1
    assert events[0].effect_on_thesis == "confirming"
    assert events[0].confidence_delta == 0.3  # |4| ∈ [3,10] → medium


# ─────────────────────────────────────────────────────────────────────────────
# E 生态设计 PR-A §3：预告类型信息量分档
# ─────────────────────────────────────────────────────────────────────────────


def _fc_typed(f_type, period="20231231", ann="20240130", lo=10.0, hi=15.0, **extra):
    """带 canonical ``forecast_type`` 的预告记录（PR-A §3 档位输入）。"""
    rec = _fc(period=period, ann=ann, lo=lo, hi=hi)
    if f_type is not None:
        rec["forecast_type"] = f_type
    rec.update(extra)
    return rec


def test_forecast_type_tier_table():
    """§3.1 三档表逐项钉死（扭亏/首亏 reversal、预增/预减/略增/略减 magnitude、续盈/续亏 inertia）。"""
    assert _FORECAST_TYPE_TIER == {
        "扭亏": "reversal",
        "首亏": "reversal",
        "预增": "magnitude",
        "预减": "magnitude",
        "略增": "magnitude",
        "略减": "magnitude",
        "续盈": "inertia",
        "续亏": "inertia",
    }
    assert _DEFAULT_TIER == "unknown"


def test_forecast_tier_fail_open():
    """缺失/空/不确定/未知取值/非 dict → unknown（fail-open，不猜档位、不抛）。"""
    assert _forecast_tier({"forecast_type": "扭亏"}) == "reversal"
    assert _forecast_tier({"forecast_type": "首亏"}) == "reversal"
    assert _forecast_tier({"forecast_type": " 续盈 "}) == "inertia"
    assert _forecast_tier({"forecast_type": "续亏"}) == "inertia"
    assert _forecast_tier({"forecast_type": "略增"}) == "magnitude"
    assert _forecast_tier({}) == "unknown"
    assert _forecast_tier({"forecast_type": None}) == "unknown"
    assert _forecast_tier({"forecast_type": ""}) == "unknown"
    assert _forecast_tier({"forecast_type": "不确定"}) == "unknown"
    assert _forecast_tier({"forecast_type": "预增（修正）"}) == "unknown"
    assert _forecast_tier(None) == "unknown"


def test_shift_strength_three_value_closure():
    """升降档只在 weak/medium/strong 三值内：STRONG 封顶、WEAK 兜底（三值纪律结构性保证）。"""
    for tier in ("reversal", "inertia", "magnitude", "unknown", None):
        for strength in (Strength.WEAK, Strength.MEDIUM, Strength.STRONG):
            assert _shift_strength(strength, tier) in (
                Strength.WEAK,
                Strength.MEDIUM,
                Strength.STRONG,
            )
    assert _shift_strength(Strength.WEAK, "reversal") is Strength.MEDIUM
    assert _shift_strength(Strength.MEDIUM, "reversal") is Strength.STRONG
    assert _shift_strength(Strength.STRONG, "reversal") is Strength.STRONG  # 升档封顶
    assert _shift_strength(Strength.STRONG, "inertia") is Strength.MEDIUM
    assert _shift_strength(Strength.MEDIUM, "inertia") is Strength.WEAK
    assert _shift_strength(Strength.WEAK, "inertia") is Strength.WEAK  # 降档不取消
    assert _shift_strength(Strength.MEDIUM, "magnitude") is Strength.MEDIUM
    assert _shift_strength(Strength.MEDIUM, "unknown") is Strength.MEDIUM


def test_beat_reversal_upshift_weak_to_medium():
    """A-升降档：扭亏 beat 偏离 4%（weak）⇒ 升一档 medium；首亏 miss 同档同理。"""
    beat = build_numeric_evidence(_exp(forecast=[_fc_typed("扭亏")], express=[_ex(np_yoy=15.6)]), symbol="600519")
    assert len(beat) == 1
    assert beat[0].effect_on_thesis == "confirming"
    assert beat[0].confidence_delta == 0.3  # 0.6/15 ≈ 4% → weak → 升档 medium

    miss = build_numeric_evidence(_exp(forecast=[_fc_typed("首亏")], express=[_ex(np_yoy=9.4)]), symbol="600519")
    assert len(miss) == 1
    assert miss[0].effect_on_thesis == "refuting"
    assert miss[0].confidence_delta == 0.3  # 低于下限 4% → weak → 升档 medium


def test_beat_reversal_upshift_capped_at_strong():
    """STRONG 升档封顶：扭亏偏离 20pp（已 strong）⇒ 仍 strong 0.5，不出三值域。"""
    events = build_numeric_evidence(_exp(forecast=[_fc_typed("扭亏")], express=[_ex(np_yoy=35.0)]), symbol="600519")
    assert len(events) == 1
    assert events[0].effect_on_thesis == "confirming"
    assert events[0].confidence_delta == 0.5


def test_beat_inertia_downshift_but_still_emits():
    """A-升降档：续盈/续亏 beat 降一档；WEAK 降档后仍产事件（不取消）。"""
    # 偏离 7pp → medium → 降档 weak
    down = build_numeric_evidence(_exp(forecast=[_fc_typed("续亏")], express=[_ex(np_yoy=22.0)]), symbol="600519")
    assert len(down) == 1
    assert down[0].confidence_delta == 0.1
    # 偏离 4% 已是 weak → 降档兜底 weak，但事件照常产出（不取消）
    floor = build_numeric_evidence(_exp(forecast=[_fc_typed("续盈")], express=[_ex(np_yoy=15.6)]), symbol="600519")
    assert len(floor) == 1
    assert floor[0].confidence_delta == 0.1


def test_beat_magnitude_and_unknown_tier_unchanged():
    """magnitude / unknown 不动档：偏离 4% → weak、7pp → medium（现行 beat 表逐字不变）。"""
    for f_type in ("预增", "预减", "略增", "略减", None):
        weak = build_numeric_evidence(_exp(forecast=[_fc_typed(f_type)], express=[_ex(np_yoy=15.6)]), symbol="600519")
        assert len(weak) == 1 and weak[0].confidence_delta == 0.1
        medium = build_numeric_evidence(_exp(forecast=[_fc_typed(f_type)], express=[_ex(np_yoy=22.0)]), symbol="600519")
        assert len(medium) == 1 and medium[0].confidence_delta == 0.3


def test_forecast_inertia_cancelled_magnitude_still_emits():
    """A-inertia 取消：续盈/续亏预告（无快报）⇒ 不产事件；同区间预减（magnitude）⇒ 照常产事件。"""
    for f_type in ("续盈", "续亏"):
        assert build_numeric_evidence(_exp(forecast=[_fc_typed(f_type)]), symbol="600519") == []
    magnitude = build_numeric_evidence(_exp(forecast=[_fc_typed("预减")]), symbol="600519")
    assert len(magnitude) == 1
    assert magnitude[0].effect_on_thesis == "confirming"  # 下限 +10 > 0
    assert magnitude[0].description.count("下限保守锚") == 1


def test_retraction_not_affected_by_tier():
    """A-retraction 不受 tier：续盈类修正预告 ⇒ retraction 照常产（且末条续盈仍不产方向事件）。"""
    old = _fc_typed("续盈", ann="20240130", lo=10.0, hi=15.0)
    corrected = _fc_typed("续盈", ann="20240220", lo=20.0, hi=30.0, forecast_update_flag="1")
    events = build_numeric_evidence(_exp(forecast=[old, corrected]), symbol="600519")

    retractions = [e for e in events if e.supersedes]
    assert len(retractions) == 1
    assert retractions[0].effect_on_thesis == "neutral"
    assert retractions[0].confidence_delta == 0.0
    assert retractions[0].supersedes == [_forecast_event_id(old, "600519")]
    # 修正后的末条是续盈（inertia）⇒ 不再产方向事件，只有 retraction 一条
    assert [e.effect_on_thesis for e in events] == ["neutral"]


# ─────────────────────────────────────────────────────────────────────────────
# E 生态设计 PR-A §4：区间保守锚（替代中点口径）
# ─────────────────────────────────────────────────────────────────────────────


def test_anchor_lower_bound_confirming():
    """B-保守锚：lo=+5/hi=+40 ⇒ confirming，value=|lo|=5（非中点 22.5），description 自证口径。"""
    events = build_numeric_evidence(_exp(forecast=[_fc(lo=5.0, hi=40.0)]), symbol="600519")
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"
    # 事件 id 携带 value=|lo|=5.0（把中点 22.5 换回来 ⇒ 本断言必红）
    assert e.id == _event_id("2024-01-30", "expectation", "600519:forecast:20231231", 5.0)
    assert e.id != _event_id("2024-01-30", "expectation", "600519:forecast:20231231", 22.5)
    assert e.confidence_delta == 0.3  # |5| ≥ 5 → medium
    assert "[+5.00%, +40.00%]" in e.description
    assert "下限保守锚" in e.description and "+5.00" in e.description


def test_anchor_upper_bound_refuting():
    """B-保守锚：lo=−50/hi=−5 ⇒ refuting，value=|hi|=5（非中点 −27.5）。"""
    events = build_numeric_evidence(_exp(forecast=[_fc(lo=-50.0, hi=-5.0)]), symbol="600519")
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "refuting"
    assert e.id == _event_id("2024-01-30", "expectation", "600519:forecast:20231231", 5.0)
    assert e.id != _event_id("2024-01-30", "expectation", "600519:forecast:20231231", -27.5)
    assert e.confidence_delta == 0.3  # |−5| ≥ 5 → medium
    assert "[-50.00%, -5.00%]" in e.description
    assert "上限保守锚" in e.description and "-5.00" in e.description


def test_anchor_cross_zero_no_event():
    """B-保守锚：跨零（lo<=0<=hi）方向不确定 ⇒ 不产事件（magnitude/unknown 均不编造方向）。"""
    assert build_numeric_evidence(_exp(forecast=[_fc(lo=-20.0, hi=30.0)]), symbol="600519") == []
    assert build_numeric_evidence(_exp(forecast=[_fc_typed("预增", lo=-20.0, hi=30.0)]), symbol="600519") == []
    # 距零更近的跨零区间同样不产（lo=0 也属跨零，非 lo>0）
    assert build_numeric_evidence(_exp(forecast=[_fc(lo=0.0, hi=10.0)]), symbol="600519") == []
    # 反向跨零（hi=0）同理
    assert build_numeric_evidence(_exp(forecast=[_fc(lo=-10.0, hi=0.0)]), symbol="600519") == []


def test_anchor_reversal_cross_zero_profit_floor():
    """B-保守锚：扭亏跨零 + 净利润下限>0 ⇒ confirming(weak)；无绝对额/非正值 ⇒ 不产事件。"""
    floor = build_numeric_evidence(
        _exp(
            forecast=[
                _fc_typed("扭亏", lo=-20.0, hi=30.0, forecast_net_profit_min=2e8),
            ]
        ),
        symbol="600519",
    )
    assert len(floor) == 1
    e = floor[0]
    assert e.effect_on_thesis == "confirming"
    assert e.confidence_delta == 0.1  # 绝对额只定方向 ⇒ 强制 weak
    assert e.id == _event_id("2024-01-30", "expectation", "600519:forecast:20231231", 2e8)
    assert "扭亏跨零" in e.description and "[-20.00%, +30.00%]" in e.description

    # 绝对额缺失 / 为 0 / 为负 ⇒ 兜底不成立，不产事件
    for net_min in (None, 0.0, -2e8):
        assert (
            build_numeric_evidence(
                _exp(
                    forecast=[
                        _fc_typed("扭亏", lo=-20.0, hi=30.0, forecast_net_profit_min=net_min),
                    ]
                ),
                symbol="600519",
            )
            == []
        )
    # 非 reversal 档即使绝对额为正也不兜底（预增跨零仍不产）
    assert (
        build_numeric_evidence(
            _exp(forecast=[_fc_typed("预增", lo=-20.0, hi=30.0, forecast_net_profit_min=2e8)]),
            symbol="600519",
        )
        == []
    )


def test_anchor_single_side_missing():
    """单边缺失：符号能定方向（lo>0 / hi<0）⇒ 保守锚照判；符号不能定 ⇒ 不产事件。"""
    only_lo = build_numeric_evidence(
        _exp(forecast=[{"period": "20231231", "ann_date": "20240130", "profit_forecast_min_change": 5.0}]),
        symbol="600519",
    )
    assert len(only_lo) == 1 and only_lo[0].effect_on_thesis == "confirming"
    assert only_lo[0].confidence_delta == 0.3

    only_hi = build_numeric_evidence(
        _exp(forecast=[{"period": "20231231", "ann_date": "20240130", "profit_forecast_max_change": -5.0}]),
        symbol="600519",
    )
    assert len(only_hi) == 1 and only_hi[0].effect_on_thesis == "refuting"

    # 只有负下限（上限未知，可能为正）⇒ 方向不确定，不产事件
    assert (
        build_numeric_evidence(
            _exp(forecast=[{"period": "20231231", "ann_date": "20240130", "profit_forecast_min_change": -5.0}]),
            symbol="600519",
        )
        == []
    )


def test_missing_or_unknown_tier_regression_pin():
    """未知/缺失 tier 走现行行为（回归钉）：与 magnitude 档完全同档同 id。"""
    for f_type in (None, "不确定", "预增"):
        beat = build_numeric_evidence(_exp(forecast=[_fc_typed(f_type)], express=[_ex(np_yoy=15.6)]), symbol="600519")
        assert len(beat) == 1 and beat[0].confidence_delta == 0.1
        forecast = build_numeric_evidence(_exp(forecast=[_fc_typed(f_type, lo=20.0, hi=30.0)]), symbol="600519")
        assert len(forecast) == 1
        assert forecast[0].effect_on_thesis == "confirming"
        assert forecast[0].confidence_delta == 0.5
        assert forecast[0].id == _event_id("2024-01-30", "expectation", "600519:forecast:20231231", 20.0)


def test_confidence_delta_three_value_discipline_under_tier_shift():
    """三值纪律专测：升降档 + 保守锚 + 绝对额兜底混合后，confidence_delta 恒在 {0.1,0.3,0.5}。"""
    exp = _exp(
        forecast=[
            _fc_typed("扭亏"),  # 20231231 beat 20pp → strong（升档封顶）
            _fc_typed("扭亏", period="20230930"),  # beat 4% → medium（升档）
            _fc_typed("续亏", period="20230630"),  # beat 7pp → weak（降档）
            _fc_typed("首亏", period="20230331", lo=-20.0, hi=30.0, forecast_net_profit_min=1e8),
            _fc_typed("预增", period="20221231", lo=-50.0, hi=-5.0),  # 上限锚 → medium
        ],
        express=[
            _ex(np_yoy=35.0),
            _ex(period="20230930", np_yoy=15.6),
            _ex(period="20230630", np_yoy=22.0),
        ],
    )
    cons = {"eps_fy1_revision_1m": 4.0, "coverage_count": 3}
    events = build_numeric_evidence(exp, cons, symbol="600519")

    deltas = {e.confidence_delta for e in events}
    assert deltas == DISCRETE_DELTAS  # 三档都被覆盖到，且无第四值
    for e in events:
        assert e.effect_on_thesis in ("confirming", "refuting")
        assert e.confidence_delta in DISCRETE_DELTAS
