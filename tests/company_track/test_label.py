"""真实赛道标签推导单元测试（COMPANY_TRACK Phase B，B1/B2/B4）。"""

from alphabee.company_track.contracts import SegmentCollection, SegmentSnapshot
from alphabee.company_track.label import (
    derive_track_label,
    detect_track_drift,
    synthesize_track_label,
)


def _seg(name, share, yoy=None, category="按产品分类", revenue=None, margin=None):
    return SegmentSnapshot(
        report_date="20251231",
        segment_name=name,
        category=category,
        revenue=revenue,
        revenue_share=share,
        revenue_yoy=yoy,
        gross_margin=margin,
        source="em",
    )


def _gigadevice_like():
    """兆易创新式产品分解（占比 % 口径）。"""
    return [
        _seg("存储芯片", 71.3, 26.4, revenue=6.57e9, margin=42.8),
        _seg("微控制器", 20.8, None, revenue=1.91e9, margin=35.8),
        _seg("传感器", 4.2, -13.1, revenue=3.9e8, margin=19.6),
        _seg("模拟产品", 3.6, None, revenue=3.3e8, margin=37.0),
    ]


# ── B1 规则层 ──────────────────────────────────────────────────────────────


def test_derive_track_label_dominant_and_fastest():
    result = derive_track_label(_gigadevice_like())
    assert result.dominant_segment == "存储芯片"
    assert result.fastest_segment == "存储芯片"  # 增速最高且占比 ≥ 5%
    assert result.track_label == "存储芯片"
    assert result.category == "按产品分类"
    assert result.candidates[0]["name"] == "存储芯片"
    assert result.candidates[0]["score"] == round(71.3 * 1.264, 2)
    assert not result.warnings


def test_weighted_score_resolves_share_growth_conflict():
    segments = [
        _seg("传统主业", 60.0, -50.0),
        _seg("高增新业务", 30.0, 30.0),
        _seg("其他小业务", 10.0, 5.0),
    ]
    result = derive_track_label(segments)
    # 60×0.5=30 < 30×1.3=39 → 加权胜出 = 高增新业务
    assert result.dominant_segment == "传统主业"
    assert result.track_label == "高增新业务"
    assert any("加权" in w for w in result.warnings)


def test_negative_growth_dominant_still_wins_when_score_highest():
    segments = [
        _seg("绝对龙头", 80.0, -10.0),
        _seg("高增小弟", 15.0, 30.0),
    ]
    result = derive_track_label(segments)
    # 80×0.9=72 > 15×1.3=19.5 → 龙头仍胜出，无切换告警
    assert result.track_label == "绝对龙头"
    assert not result.warnings


def test_other_segments_excluded():
    segments = _gigadevice_like() + [
        _seg("其他(补充)", 0.01, 244.0),
    ]
    result = derive_track_label(segments)
    assert "其他" not in result.track_label
    assert all("其他" not in c["name"] for c in result.candidates)


def test_category_preference():
    # 产品优先；只有行业分类时回退
    product_first = derive_track_label(
        [_seg("存储芯片", 71.3, 26.4, category="按产品分类"), _seg("集成电路", 99.9, 20.0, category="按行业分类")]
    )
    assert product_first.category == "按产品分类"
    assert product_first.track_label == "存储芯片"

    industry_only = derive_track_label([_seg("集成电路", 99.9, 20.0, category="按行业分类")])
    assert industry_only.category == "按行业分类"
    assert industry_only.track_label == "集成电路"


def test_share_missing_uses_revenue_with_warning():
    segments = [
        SegmentSnapshot(
            report_date="20251231",
            segment_name="云服务器",
            category="",
            revenue=2.1e10,
            revenue_yoy=40.0,
            source="tushare",
        ),
        SegmentSnapshot(
            report_date="20251231",
            segment_name="通信设备",
            category="",
            revenue=1.05e10,
            revenue_yoy=5.0,
            source="tushare",
        ),
    ]
    result = derive_track_label(segments)
    assert result.dominant_segment == "云服务器"
    assert any("占比缺失" in w for w in result.warnings)


def test_empty_input():
    result = derive_track_label([])
    assert result.track_label == ""
    assert result.dominant_segment is None
    assert any("无有效业务线" in w for w in result.warnings)


# ── B2 LLM 复核 ────────────────────────────────────────────────────────────


def _collection(segments):
    return SegmentCollection(
        symbol="603986.SH",
        segments=segments,
        source="em",
        latest_period="20251231",
    )


def test_synthesize_off_returns_rule(monkeypatch):
    rule = derive_track_label(_gigadevice_like())
    label, basis, method = synthesize_track_label(_collection(_gigadevice_like()), rule, use_llm=False)
    assert method == "rule"
    assert label == rule.track_label


def test_synthesize_llm_success(monkeypatch):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {
                    "content": '{"track_label": "存储芯片设计龙头", "override_basis": "存储芯片占比 71.3% 且同比 +26.4%"}'
                },
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    rule = derive_track_label(_gigadevice_like())
    label, basis, method = synthesize_track_label(_collection(_gigadevice_like()), rule, use_llm=True)
    assert method == "llm"
    assert label == "存储芯片设计龙头"
    assert "71.3%" in basis


def test_synthesize_llm_failure_falls_back(monkeypatch):
    import alphabee.utils.llm as llm_module

    def boom(component, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_module, "create_chat_model", boom)
    rule = derive_track_label(_gigadevice_like())
    label, basis, method = synthesize_track_label(_collection(_gigadevice_like()), rule, use_llm=True)
    assert method == "rule"
    assert label == rule.track_label


# ── B4 漂移检测 ────────────────────────────────────────────────────────────


def _annual_seg(name, share, period):
    return SegmentSnapshot(
        report_date=period,
        segment_name=name,
        category="按产品分类",
        revenue_share=share,
        source="em",
    )


def test_drift_detected_across_annual_periods():
    segments = [
        _annual_seg("通信设备", 60.0, "20241231"),
        _annual_seg("云服务器", 40.0, "20241231"),
        _annual_seg("云服务器", 65.0, "20251231"),
        _annual_seg("通信设备", 35.0, "20251231"),
    ]
    notes = detect_track_drift(segments)
    assert len(notes) == 1
    assert "通信设备 → 云服务器" in notes[0]


def test_no_drift_when_dominant_stable():
    segments = [
        _annual_seg("存储芯片", 60.0, "20241231"),
        _annual_seg("存储芯片", 65.0, "20251231"),
    ]
    assert detect_track_drift(segments) == []


def test_half_year_periods_ignored_for_drift():
    segments = [
        _annual_seg("存储芯片", 60.0, "20241231"),
        _annual_seg("微控制器", 55.0, "20250630"),  # 半年报不算主线漂移
        _annual_seg("存储芯片", 65.0, "20251231"),
    ]
    assert detect_track_drift(segments) == []


def test_drift_uses_consistent_category_across_periods():
    # 早期只有"按地区"行时不得与后期"按产品"行比较（避免把地区当主线）
    region_only_2015 = SegmentSnapshot(
        report_date="20151231", segment_name="境外地区", category="按地区分类", revenue_share=60.0, source="em"
    )
    product_2016 = SegmentSnapshot(
        report_date="20161231", segment_name="集成电路产品", category="按产品分类", revenue_share=99.9, source="em"
    )
    product_2017 = SegmentSnapshot(
        report_date="20171231", segment_name="集成电路产品", category="按产品分类", revenue_share=99.5, source="em"
    )
    notes = detect_track_drift([region_only_2015, product_2016, product_2017])
    # 2015（地区口径）不与 2016（产品口径）比较；2016 → 2017 主线未变
    assert notes == []
