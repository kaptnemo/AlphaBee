"""行业基准推导单元测试（industry-context Phase 0）。"""

from alphabee.industry.benchmarks import (
    INDUSTRY_BENCHMARK_FIELDS,
    IndustryBenchmarks,
    derive_benchmarks,
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
    assert b.revenue_yoy == 0.20          # 中位数
    assert b.avg_roe == 0.16
    assert b.avg_debt_ratio == 0.60
    assert b.avg_gross_margin == 0.35
    assert b.pe_ttm == 25.0               # 估值透传
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
    assert b.revenue_yoy == 0.30          # 只有一条有效
    assert b.avg_roe == 0.10
    assert b.avg_debt_ratio == 0.50
    assert b.avg_gross_margin is None     # 全缺


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
