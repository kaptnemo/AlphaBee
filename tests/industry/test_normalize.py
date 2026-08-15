"""行业事实归一化单元测试（industry-context Phase 1，单位契约 + 报告期对齐）。"""

from alphabee.industry.normalize import (
    assess_period_alignment,
    normalize_industry_records,
)


def _source_rows():
    """Tushare fina_indicator 经 adapter 重命名后的源单位行（百分比）。"""
    return [
        {
            "revenue_yoy": 12.3,
            "roe": 15.2,
            "debt_to_assets": 45.6,
            "gross_margin": 32.1,
            "period": "20251231",
            "ann_date": "20260428",
            "stock_code": "600519.SH",
        },
        {
            "revenue_yoy": 8.0,
            "roe": 10.0,
            "debt_to_assets": 55.0,
            "gross_margin": 28.0,
            "period": "20251231",
            "ann_date": "20260428",
            "stock_code": "000858.SZ",
        },
    ]


# ── 单位契约（B3）─────────────────────────────────────────────────────────


def test_unit_conversion_percent_to_ratio():
    records = normalize_industry_records(_source_rows(), source="tushare")
    assert len(records) == 2
    first = records[0]
    # 百分比 → RATIO（÷100）
    assert first["roe"] == 0.152
    assert first["debt_ratio"] == 0.456
    assert first["gross_margin"] == 0.321
    # 营收增速保持百分比（百分点口径，与公司侧 revenue_yoy 一致）
    assert first["revenue_yoy"] == 12.3


def test_period_metadata_captured():
    records = normalize_industry_records(_source_rows(), source="tushare")
    first = records[0]
    assert first["end_date"] == "20251231"
    assert first["ann_date"] == "20260428"
    assert first["ts_code"] == "600519.SH"


def test_rows_without_numeric_fields_filtered():
    rows = [{"roe": 10.0, "period": "20251231"}, {"foo": 1}, {"bar": "x"}, "not-a-dict"]
    records = normalize_industry_records(rows, source="tushare")
    assert len(records) == 1
    assert records[0]["roe"] == 0.10
    assert records[0]["revenue_yoy"] is None  # 缺失字段为 None


def test_nan_and_invalid_values_become_none():
    rows = [{"roe": float("nan"), "debt_to_assets": "oops", "gross_margin": 30.0}]
    records = normalize_industry_records(rows, source="tushare")
    assert records[0]["roe"] is None
    assert records[0]["debt_ratio"] is None
    assert records[0]["gross_margin"] == 0.30


def test_valuation_passthrough_without_conversion():
    # daily_basic 估值（pe_ttm / pb_ratio）已是 RATIO，原样透传
    rows = [
        {"roe": 10.0, "period": "20251231", "pe_ttm": 25.3, "pb_ratio": 6.1},
        {"roe": 12.0, "period": "20251231", "pe_ttm": None},
    ]
    records = normalize_industry_records(rows, source="tushare")
    assert records[0]["pe_ttm"] == 25.3
    assert records[0]["pb_ratio"] == 6.1
    assert records[1]["pe_ttm"] is None


def test_unknown_source_yields_no_records():
    assert normalize_industry_records([{"roe": 10.0}], source="akshare") == []


# ── 报告期对齐（B3 周期部分）──────────────────────────────────────────────


def _record(end_date):
    return {"revenue_yoy": 10.0, "roe": 0.1, "end_date": end_date}


def test_aligned_single_period():
    records = [_record("20251231"), _record("20251231")]
    alignment = assess_period_alignment(records)
    assert alignment.status == "aligned"
    assert alignment.growth_usable() is True


def test_mostly_aligned_dominant_period():
    records = [_record("20251231")] * 8 + [_record("20250630")] * 2
    alignment = assess_period_alignment(records)
    assert alignment.status == "mostly_aligned"
    assert alignment.dominant_period == "20251231"
    assert alignment.growth_usable() is True


def test_mixed_periods_block_growth():
    records = [_record("20251231"), _record("20250630")]
    alignment = assess_period_alignment(records)
    assert alignment.status == "mixed"
    assert alignment.growth_usable() is False


def test_no_period_info_is_mixed():
    # 无报告期信息 = 无法确认对齐 → 从严置 mixed（B3：无法对齐 → 置空）
    records = [{"revenue_yoy": 10.0, "roe": 0.1}]
    alignment = assess_period_alignment(records)
    assert alignment.status == "mixed"
    assert alignment.growth_usable() is False
