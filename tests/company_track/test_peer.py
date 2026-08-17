"""对标组基准推导测试（COMPANY_TRACK Phase D，D1/D2/D5）。"""

import pytest

import alphabee.company_track.peer as peer_module
from alphabee.company_track import derive_peer_benchmarks


def _source_rows():
    """对标组成分股源单位行（百分比）+ 估值（已是 RATIO）。"""
    return [
        {
            "revenue_yoy": 10.0,
            "roe": 12.0,
            "debt_to_assets": 40.0,
            "gross_margin": 30.0,
            "pe_ttm": 20.0,
            "pb_ratio": 3.0,
            "period": "20251231",
        },
        {
            "revenue_yoy": 20.0,
            "roe": 16.0,
            "debt_to_assets": 60.0,
            "gross_margin": 35.0,
            "pe_ttm": 30.0,
            "pb_ratio": 5.0,
            "period": "20251231",
        },
        {
            "revenue_yoy": 30.0,
            "roe": 20.0,
            "debt_to_assets": 80.0,
            "gross_margin": 40.0,
            "pe_ttm": 40.0,
            "pb_ratio": 7.0,
            "period": "20251231",
        },
    ]


def _patch_fetch(monkeypatch, rows=None, codes=None, error=None):
    import alphabee.industry.data as data_module

    monkeypatch.setattr(
        data_module,
        "fetch_peer_financials_for_codes",
        lambda codes, limit: (rows or [], codes or [], error),
    )


def test_derive_peer_benchmarks_median_semantics(monkeypatch):
    _patch_fetch(monkeypatch, _source_rows(), ["A", "B", "C"])
    values, meta = derive_peer_benchmarks(["A", "B", "C"])
    # 中位数语义与行业基准完全一致；单位契约：roe/debt/gross 为 RATIO，增速为百分点
    assert values["peer_avg_roe"] == 0.16
    assert values["peer_avg_debt_ratio"] == 0.60
    assert values["peer_avg_gross_margin"] == 0.35
    assert values["peer_revenue_yoy"] == 20.0  # 百分点口径（与 industry_revenue_yoy 一致）
    assert values["peer_median_pe_ttm"] == 30.0
    assert values["peer_median_pb"] == 5.0
    assert meta["peer_count"] == 3
    assert meta["error"] is None


def test_derive_peer_benchmarks_missing_fields_not_injected(monkeypatch):
    rows = [{"roe": 10.0}, {"roe": 20.0}]  # 源单位百分比 → ÷100；无估值/增速字段
    _patch_fetch(monkeypatch, rows, ["A", "B"])
    values, meta = derive_peer_benchmarks(["A", "B"])
    assert values["peer_avg_roe"] == pytest.approx(0.15)  # 有
    assert "peer_revenue_yoy" not in values  # 缺失不注入（回退 industry_*）
    assert "peer_median_pe_ttm" not in values


def test_derive_peer_benchmarks_fetch_error(monkeypatch):
    _patch_fetch(monkeypatch, [], [], "对标组财务指标均取数失败")
    values, meta = derive_peer_benchmarks(["A", "B"])
    assert values == {}
    assert "失败" in meta["error"]


def test_derive_peer_benchmarks_empty_codes(monkeypatch):
    _patch_fetch(monkeypatch, [], [], "对标组代码列表为空")
    values, meta = derive_peer_benchmarks([])
    assert values == {}
    assert meta["peer_count"] == 0


def test_peer_benchmark_fields_catalog():
    fields = peer_module.peer_benchmark_fields()
    assert set(fields) == {
        "peer_avg_roe",
        "peer_avg_debt_ratio",
        "peer_avg_gross_margin",
        "peer_revenue_yoy",
        "peer_median_pe_ttm",
        "peer_median_pb",
    }
