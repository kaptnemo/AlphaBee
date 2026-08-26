"""衍生规则行业基准阈值回退链测试（industry-context Phase 0，见 3.0）。

契约：
- 行业基准字段存在时走相对判断（银行高杠杆=行业常态、制造业相对判断）；
- 行业基准字段缺失时顺延到绝对阈值（回退链），行为与改造前一致。
"""

from alphabee.agents.derived_facts.registry import RULES, load_rules


def _compute(rule_name: str, facts: dict[str, float]) -> str:
    load_rules()
    return RULES[rule_name].compute(dict(facts))["level"]


# ── debt_ratio ─────────────────────────────────────────────────────────────


def test_debt_ratio_bank_norm_is_not_aggressive():
    # 银行 92% 负债率 vs 银行行业均值 0.92 → 行业常态 → moderate（改造前为 aggressive）
    assert (
        _compute("debt_ratio", {"total_liabilities": 0.92, "total_assets": 1.0, "industry_avg_debt_ratio": 0.92})
        == "moderate"
    )


def test_debt_ratio_manufacturing_above_industry_is_aggressive():
    assert (
        _compute("debt_ratio", {"total_liabilities": 0.72, "total_assets": 1.0, "industry_avg_debt_ratio": 0.45})
        == "aggressive"
    )


def test_debt_ratio_missing_industry_falls_back_to_absolute():
    # 无行业字段 → 回退绝对阈值，行为与改造前一致
    assert _compute("debt_ratio", {"total_liabilities": 0.92, "total_assets": 1.0}) == "aggressive"
    assert _compute("debt_ratio", {"total_liabilities": 0.55, "total_assets": 1.0}) == "moderate"
    assert _compute("debt_ratio", {"total_liabilities": 0.30, "total_assets": 1.0}) == "conservative"


# ── roe_level ──────────────────────────────────────────────────────────────


def test_roe_level_relative_to_industry():
    # 制造业 ROE=6% vs 行业均值 5% → good（改造前为 weak）
    assert (
        _compute("roe_level", {"net_profit": 0.06, "avg_shareholders_equity": 1.0, "industry_avg_roe": 0.05}) == "good"
    )
    # ROE 明显高于行业 → excellent
    assert (
        _compute("roe_level", {"net_profit": 0.10, "avg_shareholders_equity": 1.0, "industry_avg_roe": 0.05})
        == "excellent"
    )


def test_roe_level_missing_industry_falls_back():
    assert _compute("roe_level", {"net_profit": 0.06, "avg_shareholders_equity": 1.0}) == "weak"
    assert _compute("roe_level", {"net_profit": 0.18, "avg_shareholders_equity": 1.0}) == "excellent"


# ── peg_ratio（Phase 0 决策：保持绝对阈值，行业判断留给 signal 层）──────


def test_peg_ratio_remains_absolute():
    # PEG 已按公司自身成长归一化，Phase 0 不做行业相对判断
    assert _compute("peg_ratio", {"pe_ttm": 40.0, "net_profit_yoy": 16.0}) == "overvalued"
    assert _compute("peg_ratio", {"pe_ttm": 20.0, "net_profit_yoy": 20.0}) == "fair"
    assert _compute("peg_ratio", {"pe_ttm": 10.0, "net_profit_yoy": 20.0}) == "undervalued"


# ── market_share_change 复活 ───────────────────────────────────────────────


def test_market_share_change_computes_when_industry_field_present():
    # 注入 industry_revenue_yoy（PERCENT 单位）后规则从 invalid 恢复
    assert _compute("market_share_change", {"revenue_yoy": 25.0, "industry_revenue_yoy": 12.0}) == "gaining"
    assert _compute("market_share_change", {"revenue_yoy": 5.0, "industry_revenue_yoy": 12.0}) == "losing"
    assert _compute("market_share_change", {"revenue_yoy": 15.0, "industry_revenue_yoy": 12.0}) == "stable"


def test_market_share_change_still_blocked_without_industry_field():
    # 无行业字段 → 公式求值抛 Unknown variable → invalid（保持改造前行为）
    assert _compute("market_share_change", {"revenue_yoy": 25.0}) == "invalid"
