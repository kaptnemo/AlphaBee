"""业务线取数测试（COMPANY_TRACK Phase A，A3/A5：EM 优先、fina_mainbz 兜底）。"""

import alphabee.company_track.data as data_module
from alphabee.company_track import fetch_business_segments


def _em_rows():
    return [
        {
            "report_date": "20251231",
            "biz_segment_name": "云计算/服务器",
            "biz_segment_revenue": 2.1e10,
            "biz_segment_revenue_share": 0.667,
            "biz_gross_margin": 9.5,
        },
        {
            "report_date": "20251231",
            "biz_segment_name": "通信设备",
            "biz_segment_revenue": 1.05e10,
            "biz_segment_revenue_share": 0.333,
        },
        {
            "report_date": "20241231",
            "biz_segment_name": "云计算/服务器",
            "biz_segment_revenue": 1.5e10,
            "biz_segment_revenue_share": 0.60,
        },
        {
            "report_date": "20241231",
            "biz_segment_name": "通信设备",
            "biz_segment_revenue": 1.0e10,
            "biz_segment_revenue_share": 0.40,
        },
    ]


def _tushare_rows():
    return [
        {"period": "20251231", "biz_segment_name": "云计算/服务器", "biz_segment_revenue": 2.1e10},
        {"period": "20251231", "biz_segment_name": "通信设备", "biz_segment_revenue": 1.05e10},
        {"period": "20241231", "biz_segment_name": "云计算/服务器", "biz_segment_revenue": 1.5e10},
        {"period": "20241231", "biz_segment_name": "通信设备", "biz_segment_revenue": 1.0e10},
    ]


def test_em_primary_source(monkeypatch):
    monkeypatch.setattr(data_module, "_fetch_em_rows", lambda symbol: (_em_rows(), None))
    monkeypatch.setattr(data_module, "_fetch_tushare_rows", lambda symbol: ([], "不应调用"))

    result = fetch_business_segments("603986.SH")
    assert result.source == "em"
    assert result.latest_period == "20251231"
    assert len(result.segments) == 4  # 两个报告期 × 两个分项（跨期 yoy 推导前提）
    assert len(result.latest_segments()) == 2
    assert result.error is None
    cloud = next(s for s in result.latest_segments() if s.segment_name == "云计算/服务器")
    assert cloud.revenue_share == 66.7
    assert cloud.revenue_yoy == 40.0  # (2.1/1.5 − 1) × 100，跨期推导
    assert cloud.is_calculated is True  # yoy 由推导给出


def test_tushare_fallback_when_em_fails(monkeypatch):
    monkeypatch.setattr(data_module, "_fetch_em_rows", lambda symbol: ([], "东方财富挂了"))
    monkeypatch.setattr(data_module, "_fetch_tushare_rows", lambda symbol: (_tushare_rows(), None))

    result = fetch_business_segments("603986.SH")
    assert result.source == "tushare"
    assert result.error is None
    assert len(result.segments) == 4
    assert result.segments[0].revenue_share is None  # tushare 兜底不推导占比
    assert result.segments[0].revenue_yoy == 40.0  # 跨期 yoy 仍推导


def test_both_sources_fail_returns_none_with_error(monkeypatch):
    monkeypatch.setattr(data_module, "_fetch_em_rows", lambda symbol: ([], "em err"))
    monkeypatch.setattr(data_module, "_fetch_tushare_rows", lambda symbol: ([], "fina_mainbz err"))

    result = fetch_business_segments("603986.SH")
    assert result.source == "none"
    assert result.segments == []
    assert result.latest_period == ""
    assert result.error == "fina_mainbz err"  # 兜底源的错误留痕


def test_filters_applied(monkeypatch):
    monkeypatch.setattr(data_module, "_fetch_em_rows", lambda symbol: (_em_rows(), None))
    result = fetch_business_segments("603986.SH", min_share=40.0)
    latest_names = [s.segment_name for s in result.latest_segments()]
    assert latest_names == ["云计算/服务器"]  # 通信设备 33.3 < 40 被过滤


def test_akshare_symbol_conversion():
    assert data_module._akshare_symbol("603986.SH") == "SH603986"
    assert data_module._akshare_symbol("000001.SZ") == "SZ000001"
    assert data_module._akshare_symbol("SH600519") == "SH600519"
    assert data_module._akshare_symbol("600519") == "SH600519"


def test_tushare_rows_deduplicated_by_update_flag():
    rows = [
        {"period": "20251231", "biz_segment_name": "集成电路", "biz_segment_revenue": 1e10, "update_flag": "1"},
        {
            "period": "20251231",
            "biz_segment_name": "集成电路",
            "biz_segment_revenue": 9.5e9,
            "update_flag": "2",
        },  # 修订版优先
        {"period": "20251231", "biz_segment_name": "存储芯片", "biz_segment_revenue": 3e9, "update_flag": "1"},
    ]
    deduped = data_module._dedupe_tushare_rows(rows)
    assert len(deduped) == 2
    ic = next(r for r in deduped if r["biz_segment_name"] == "集成电路")
    assert ic["biz_segment_revenue"] == 9.5e9  # update_flag 2 胜出
