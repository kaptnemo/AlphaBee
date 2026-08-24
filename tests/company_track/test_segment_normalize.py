"""业务线归一化单元测试（COMPANY_TRACK Phase A，A4/A5）。"""

from alphabee.company_track.normalize import (
    assess_period_consistency,
    derive_segment_yoy,
    latest_report_period,
    normalize_segments,
    segments_for_period,
)

# ── EM 源：占比/毛利率直接给出，yoy 跨期推导 ────────────────────────────────


def _em_rows():
    return [
        {
            "report_date": "20251231",
            "biz_segment_category": "按产品分类",
            "biz_segment_name": "云计算/服务器",
            "biz_segment_revenue": 2.1e10,
            "biz_segment_revenue_share": 0.667,
            "biz_segment_cost": 1.9e10,
            "biz_segment_profit": 2.0e9,
            "biz_gross_margin": 0.095,
        },
        {
            "report_date": "20251231",
            "biz_segment_category": "按产品分类",
            "biz_segment_name": "通信设备",
            "biz_segment_revenue": 1.05e10,
            "biz_segment_revenue_share": 0.333,
            "biz_segment_cost": 9.5e9,
            "biz_segment_profit": 1.0e9,
            "biz_gross_margin": 0.095,
        },
        {
            "report_date": "20241231",
            "biz_segment_category": "按产品分类",
            "biz_segment_name": "云计算/服务器",
            "biz_segment_revenue": 1.5e10,
            "biz_segment_revenue_share": 0.60,
            "biz_gross_margin": 0.08,
        },
        {
            "report_date": "20241231",
            "biz_segment_category": "按产品分类",
            "biz_segment_name": "通信设备",
            "biz_segment_revenue": 1.0e10,
            "biz_segment_revenue_share": 0.40,
            "biz_gross_margin": 0.09,
        },
    ]


def test_em_normalize_keeps_direct_share_and_derives_yoy():
    segments = normalize_segments(_em_rows(), "em")
    assert len(segments) == 4
    latest = segments_for_period(segments, "20251231")
    assert len(latest) == 2

    cloud = next(s for s in latest if s.segment_name == "云计算/服务器")
    assert cloud.revenue == 2.1e10
    assert cloud.revenue_share == 66.7  # 数据源 0-1 比例 → ×100
    assert cloud.gross_margin == 9.5
    assert cloud.revenue_yoy == 40.0  # (2.1/1.5 − 1) × 100
    assert cloud.is_calculated is True  # yoy 由推导给出


def test_yoy_requires_prior_year_same_segment():
    rows = _em_rows() + [
        {
            "report_date": "20251231",
            "biz_segment_name": "新增AI业务",
            "biz_segment_revenue": 3e9,
            "biz_segment_revenue_share": 0.05,
        }
    ]
    segments = normalize_segments(rows, "em")
    new_seg = next(s for s in segments if s.segment_name == "新增AI业务")
    assert new_seg.revenue_yoy is None  # 无去年同期 → 无法推导


def test_latest_period_and_consistency():
    segments = normalize_segments(_em_rows(), "em")
    assert latest_report_period(segments) == "20251231"
    status, counts = assess_period_consistency(segments)
    assert status == "multi_period"  # 多报告期是跨期 yoy 推导的前提，属正常
    assert counts == {"20251231": 2, "20241231": 2}
    assert assess_period_consistency([]) == ("empty", {})


# ── Tushare 源：无占比/增速列；占比不推导（口径无法区分），yoy 跨期推导 ──────


def _tushare_rows():
    return [
        {
            "period": "20251231",
            "biz_segment_name": "云计算/服务器",
            "biz_segment_revenue": 2.1e10,
            "biz_segment_cost": 1.9e10,
            "biz_segment_profit": 2.0e9,
        },
        {
            "period": "20251231",
            "biz_segment_name": "通信设备",
            "biz_segment_revenue": 1.05e10,
            "biz_segment_cost": 9.5e9,
            "biz_segment_profit": 1.0e9,
        },
        {"period": "20241231", "biz_segment_name": "云计算/服务器", "biz_segment_revenue": 1.5e10},
        {"period": "20241231", "biz_segment_name": "通信设备", "biz_segment_revenue": 1.0e10},
    ]


def test_tushare_share_not_derived_yoy_derived():
    # 实测修正：fina_mainbz 产品/地区混列且无分类类型标记，推导占比会口径错配
    # （兆易创新"集成电路产品"被算成 ~1%），故 tushare 兜底占比保持 None（宁缺毋错）
    segments = normalize_segments(_tushare_rows(), "tushare")
    latest = segments_for_period(segments, "20251231")
    cloud = next(s for s in latest if s.segment_name == "云计算/服务器")
    assert cloud.revenue_share is None
    assert cloud.revenue_yoy == 40.0  # 跨期推导仍生效
    assert cloud.is_calculated is True  # yoy 由推导给出
    assert cloud.source == "tushare"


def test_unknown_source_yields_empty():
    assert normalize_segments(_em_rows(), "akshare") == []


def test_rows_without_revenue_dropped():
    rows = [{"report_date": "20251231", "biz_segment_name": "无收入项"}]
    assert normalize_segments(rows, "em") == []


# ── 噪音过滤 ───────────────────────────────────────────────────────────────


def test_min_share_filter():
    segments = normalize_segments(_em_rows(), "em", min_share=40.0)
    latest = segments_for_period(segments, "20251231")
    names = [s.segment_name for s in latest]
    assert "云计算/服务器" in names
    assert "通信设备" not in names  # 33.3 < 40


def test_drop_other_filter():
    rows = _em_rows() + [
        {
            "report_date": "20251231",
            "biz_segment_name": "其他业务",
            "biz_segment_revenue": 1e8,
            "biz_segment_revenue_share": 0.3,
        },
    ]
    segments = normalize_segments(rows, "em", drop_other=True)
    assert not any("其他" in s.segment_name for s in segments)


def test_drop_other_keeps_compound_name():
    # 「其他」精确判定：不得误杀「汽车、汽车相关产品及其他产品」这类复合名（比亚迪）
    rows = [
        {
            "report_date": "20251231",
            "biz_segment_name": "汽车、汽车相关产品及其他产品",
            "biz_segment_revenue": 5e10,
            "biz_segment_revenue_share": 0.8,
        },
        {
            "report_date": "20251231",
            "biz_segment_name": "其他(补充)",
            "biz_segment_revenue": 1e8,
            "biz_segment_revenue_share": 0.001,
        },
    ]
    segments = normalize_segments(rows, "em", drop_other=True)
    names = [s.segment_name for s in segments]
    assert "汽车、汽车相关产品及其他产品" in names
    assert "其他(补充)" not in names


# ── 跨期推导纯函数 ─────────────────────────────────────────────────────────


def test_derive_segment_yoy_half_year_periods():
    from alphabee.company_track.contracts import SegmentSnapshot

    segments = [
        SegmentSnapshot(report_date="20250630", segment_name="A", revenue=120.0),
        SegmentSnapshot(report_date="20240630", segment_name="A", revenue=100.0),
        SegmentSnapshot(report_date="20250331", segment_name="B", revenue=50.0),
    ]
    derive_segment_yoy(segments)
    assert next(s for s in segments if s.report_date == "20250630").revenue_yoy == 20.0
    # 20250331 → 去年同报告期 20240331 无数据 → None
    assert next(s for s in segments if s.report_date == "20250331").revenue_yoy is None
