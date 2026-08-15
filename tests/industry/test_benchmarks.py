"""行业基准推导单元测试（industry-context Phase 0 + Phase 1 类别分组）。"""

from alphabee.industry.benchmarks import (
    BENCHMARK_CATEGORIES,
    INDUSTRY_BENCHMARK_FIELDS,
    IndustryBenchmarks,
    derive_benchmarks,
    flatten_benchmarks,
    group_benchmarks,
)


def test_median_of_peer_records():
    peers = [
        {"revenue_yoy": 0.10, "roe": 0.12, "debt_ratio": 0.40, "gross_margin": 0.30},
        {"revenue_yoy": 0.20, "roe": 0.16, "debt_ratio": 0.60, "gross_margin": 0.35},
        {"revenue_yoy": 0.30, "roe": 0.20, "debt_ratio": 0.80, "gross_margin": 0.40},
    ]
    b = derive_benchmarks(peers, industry="白酒", sw_code="801120.SI", pe_ttm=25.0, pb=6.0)
    assert b.industry == "白酒"
    assert b.peer_count == 3
    assert b.revenue_yoy == 0.20  # 中位数
    assert b.avg_roe == 0.16
    assert b.avg_debt_ratio == 0.60
    assert b.avg_gross_margin == 0.35
    assert b.pe_ttm == 25.0  # 估值透传
    assert b.pb == 6.0


def test_median_even_count_averages_middle_two():
    peers = [
        {"roe": 0.10},
        {"roe": 0.20},
        {"roe": 0.30},
        {"roe": 0.40},
    ]
    b = derive_benchmarks(peers, industry="x")
    assert b.avg_roe == 0.25


def test_none_and_missing_fields_skipped():
    peers = [
        {"revenue_yoy": None, "roe": 0.10},
        {"roe": "invalid", "debt_ratio": 0.50},
        {"revenue_yoy": 0.30},  # 缺 roe
    ]
    b = derive_benchmarks(peers, industry="x")
    assert b.revenue_yoy == 0.30  # 只有一条有效
    assert b.avg_roe == 0.10
    assert b.avg_debt_ratio == 0.50
    assert b.avg_gross_margin is None  # 全缺


def test_all_missing_yields_none_and_no_injection():
    b = derive_benchmarks([], industry="x")
    assert b.revenue_yoy is None
    assert b.avg_roe is None
    assert b.has_financial_benchmarks() is False
    assert b.to_fact_values() == {}


def test_to_fact_values_only_injects_present_values():
    b = IndustryBenchmarks(
        industry="x",
        revenue_yoy=0.12,
        avg_roe=None,  # 缺失不注入
        avg_debt_ratio=0.45,
        avg_gross_margin=None,
        pe_ttm=18.5,
        pb=None,
    )
    values = b.to_fact_values()
    assert values == {
        "industry_revenue_yoy": 0.12,
        "industry_avg_debt_ratio": 0.45,
        "industry_pe_ttm": 18.5,
    }
    assert "industry_avg_roe" not in values
    assert "industry_pb" not in values


def test_benchmark_fields_catalog_consistent():
    # 目录字段都能注入 fact_values（防拼写漂移）
    b = IndustryBenchmarks(
        industry="x",
        revenue_yoy=0.12,
        avg_roe=0.15,
        avg_debt_ratio=0.45,
        avg_gross_margin=0.30,
    )
    values = b.to_fact_values()
    for name in INDUSTRY_BENCHMARK_FIELDS:
        assert name in values, f"{name} 缺失"


# ── Phase 1：类别分组 ─────────────────────────────────────────────────────


def test_benchmark_categories_cover_all_injection_keys():
    # 每个可注入 fact_values 的 canonical 键都有类别归属（防新字段漏分组）

    injectable = {
        *INDUSTRY_BENCHMARK_FIELDS,
        "industry_pe_ttm",
        "industry_pb",
    }
    assert injectable <= set(BENCHMARK_CATEGORIES)


def test_group_benchmarks_splits_by_category():
    flat = {
        "industry_pe_ttm": 18.5,
        "industry_pb": 2.1,
        "industry_avg_roe": 0.15,
        "industry_avg_debt_ratio": None,  # None 保留在分组里
        "industry_revenue_yoy": 12.3,
        "unknown_key": 1.0,  # 未知键丢弃
    }
    valuation, financial, growth = group_benchmarks(flat)
    assert valuation == {"industry_pe_ttm": 18.5, "industry_pb": 2.1}
    assert financial == {"industry_avg_roe": 0.15, "industry_avg_debt_ratio": None}
    assert growth == {"industry_revenue_yoy": 12.3}
    assert "unknown_key" not in valuation and "unknown_key" not in financial


def test_flatten_benchmarks_is_inverse_of_group():
    valuation = {"industry_pe_ttm": 18.5}
    financial = {"industry_avg_roe": 0.15, "industry_avg_debt_ratio": None}
    growth = {"industry_revenue_yoy": 12.3}
    flat = flatten_benchmarks(valuation, financial, growth)
    assert flat == {
        "industry_pe_ttm": 18.5,
        "industry_avg_roe": 0.15,
        "industry_avg_debt_ratio": None,
        "industry_revenue_yoy": 12.3,
    }
    assert group_benchmarks(flat) == (valuation, financial, growth)


def test_to_category_dicts_from_benchmarks():
    b = IndustryBenchmarks(
        industry="x",
        revenue_yoy=12.3,
        avg_roe=0.15,
        avg_debt_ratio=None,
        pe_ttm=25.0,
        pb=None,
    )
    valuation, financial, growth = b.to_category_dicts()
    assert valuation == {"industry_pe_ttm": 25.0, "industry_pb": None}
    assert financial == {
        "industry_avg_roe": 0.15,
        "industry_avg_debt_ratio": None,
        "industry_avg_gross_margin": None,
    }
    assert growth == {"industry_revenue_yoy": 12.3}


# ── Phase 1：成分股中位数估值（优先于指数快照）────────────────────────────


def test_peer_median_valuation_wins_over_snapshot():
    peers = [
        {"roe": 0.10, "pe_ttm": 20.0, "pb_ratio": 3.0},
        {"roe": 0.12, "pe_ttm": 30.0, "pb_ratio": 5.0},
        {"roe": 0.14, "pe_ttm": 40.0, "pb_ratio": 7.0},
    ]
    b = derive_benchmarks(peers, industry="x", pe_ttm=25.0, pb=2.0)
    assert b.pe_ttm == 30.0  # 中位数优先
    assert b.pb == 5.0
    assert "valuation:peer_median" in b.source_refs


def test_negative_pe_filtered_from_median():
    # 亏损股负 PE 无估值水平意义，剔除避免扭曲中位数
    peers = [
        {"pe_ttm": 20.0},
        {"pe_ttm": -15.0},
        {"pe_ttm": 30.0},
        {"pe_ttm": 40.0},
    ]
    b = derive_benchmarks(peers, industry="x")
    assert b.pe_ttm == 30.0  # 只统计正值 [20, 30, 40] 的中位数


def test_snapshot_fallback_when_no_peer_valuation():
    peers = [{"roe": 0.10}, {"roe": 0.12}]
    b = derive_benchmarks(peers, industry="x", pe_ttm=25.0, pb=2.0)
    assert b.pe_ttm == 25.0
    assert b.pb == 2.0
    assert "valuation:peer_median" not in b.source_refs
