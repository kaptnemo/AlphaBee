"""行业知识契约单元测试（industry-context Phase 1，artifact v2）。"""

from alphabee.industry.contracts import (
    IndustryContextArtifact,
    IndustryTarget,
    PeriodAlignment,
)


def _sample_artifact(**overrides) -> IndustryContextArtifact:
    kwargs = dict(
        industry="白酒",
        classification_standard="sw_l1",
        industry_code="801120.SI",
        sw_code="801120.SI",
        as_of_date="2026-08-15",
        valuation_benchmarks={"industry_pe_ttm": 25.0, "industry_pb": 6.0},
        financial_benchmarks={"industry_avg_roe": 0.15, "industry_avg_debt_ratio": None},
        growth_benchmarks={"industry_revenue_yoy": 12.3},
        peer_count=20,
    )
    kwargs.update(overrides)
    return IndustryContextArtifact(**kwargs)


# ── 序列化往返 ─────────────────────────────────────────────────────────────


def test_roundtrip_dump_validate():
    artifact = _sample_artifact()
    restored = IndustryContextArtifact.model_validate(artifact.model_dump(mode="json"))
    assert restored == artifact
    assert restored.schema_version == "2"


def test_defaults_fresh():
    artifact = IndustryContextArtifact()
    assert artifact.schema_version == "2"
    assert artifact.degraded is False
    assert artifact.stale is False
    assert artifact.review_status is None
    assert artifact.benchmark_fact_values() == {}


# ── benchmark 展平 ─────────────────────────────────────────────────────────


def test_benchmark_fact_values_flattens_and_drops_none():
    artifact = _sample_artifact()
    values = artifact.benchmark_fact_values()
    assert values == {
        "industry_pe_ttm": 25.0,
        "industry_pb": 6.0,
        "industry_avg_roe": 0.15,
        "industry_revenue_yoy": 12.3,
    }
    # None 不注入（缺失即回退默认阈值）
    assert "industry_avg_debt_ratio" not in values


def test_all_benchmarks_keeps_none_placeholders():
    artifact = _sample_artifact()
    merged = artifact.all_benchmarks()
    assert merged["industry_avg_debt_ratio"] is None
    assert merged["industry_revenue_yoy"] == 12.3


def test_present_benchmark_categories():
    artifact = _sample_artifact(
        lifecycle_stage="成熟期",
        business_model_summary="",
        financial_benchmarks={"industry_avg_roe": 0.15},
        growth_benchmarks={},
    )
    assert artifact.present_benchmark_categories() == {"valuation", "financial", "qualitative"}

    empty = IndustryContextArtifact()
    assert empty.present_benchmark_categories() == set()


# ── IndustryTarget ─────────────────────────────────────────────────────────


def test_target_forms():
    assert IndustryTarget(symbol="600519.SH").is_direct() is False
    assert IndustryTarget(symbol="600519.SH").describe() == "symbol=600519.SH"
    direct = IndustryTarget(classification_standard="sw_l1", industry_code="801120.SI", industry_name="白酒")
    assert direct.is_direct() is True
    assert direct.describe() == "sw_l1:801120.SI"


# ── PeriodAlignment ────────────────────────────────────────────────────────


def test_period_alignment_growth_usable():
    assert PeriodAlignment(status="aligned").growth_usable() is True
    assert PeriodAlignment(status="mostly_aligned").growth_usable() is True
    assert PeriodAlignment(status="mixed").growth_usable() is False
