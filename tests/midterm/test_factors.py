"""factors.py 的单元测试：七维度映射、嵌套结构提取、缺失降级契约、字段治理与 round-trip。

全部用 mock / 合成数据，不发起真实网络请求（fact tools / collectors / market_regime
的数据源函数在 ``get_factor_snapshot`` 编排层被 monkeypatch 替换）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import alphabee.midterm.factors as factors_mod
from alphabee.midterm import (
    build_crowding_factor,
    build_expectation_factor,
    build_fundamental_factor,
    build_market_factor,
    build_risk_factor,
    build_trend_factor,
    build_valuation_factor,
    get_factor_snapshot,
)
from alphabee.midterm.models import FactorSnapshot

# ─────────────────────────────────────────────────────────────────────────────
# 合成数据
# ─────────────────────────────────────────────────────────────────────────────


def _fin() -> dict[str, Any]:
    return {
        "fina": [
            {
                "revenue_yoy": 10.5,
                "net_profit_yoy": 20.0,
                "eps_growth_yoy": 18.0,
                "roe": 15.2,
                "gross_margin": 40.0,
                "net_margin": 12.0,
                "free_cashflow": 1e8,
                "debt_to_assets": 45.0,
                "current_ratio": 1.8,
            }
        ],
        "balance": [{"goodwill": 5e7}],
        "cashflow": [{"operating_cashflow": 2e8}],
    }


def _market() -> dict[str, Any]:
    return {
        "latest_daily": {"price_change_pct": 2.5},
        "latest_daily_basic": {"pe_ttm": 20.0, "pb_ratio": 3.0, "turnover_rate": 3.5},
        "ma": {"ma20": 100.0, "ma60": 95.0, "ma120": 90.0},
        "pe_ttm_5y_avg": 25.0,
        "pe_ttm_5y_percentile": 0.3,
        "pb_5y_percentile": 0.4,
        "rs_stock_market_20d": 3.0,
        "rs_stock_market_60d": 5.0,
    }


def _industry() -> dict[str, Any]:
    return {
        "rs_stock_industry_20d": 1.0,
        "rs_stock_industry_60d": 2.0,
        "rs_industry_market_20d": 4.0,
        "rs_industry_market_60d": 6.0,
        "industry_breadth_20": 60.0,
        "industry_breadth_60": 55.0,
        "sw_daily": [{"industry_pe_ttm": 18.0, "industry_pb": 2.0}],
    }


def _consensus(**overrides: Any) -> SimpleNamespace:
    values = {
        "eps_fy1": 3.2,
        "eps_fy2": 4.0,
        "eps_fy3": 4.8,
        "target_price": 100.0,
        "rating_mean": 2.5,
        "coverage_count": 20,
        "eps_fy1_revision_1m": 1.0,
        "eps_fy1_revision_3m": 2.0,
        "eps_fy2_revision_1m": 0.5,
        "revision_breadth": 0.6,
        "revision_acceleration": 0.1,
        "rating_upgrade_1m": 3,
        "rating_downgrade_1m": 1,
    }
    values.update(overrides)
    return SimpleNamespace(values=values)


def _crowding(**overrides: Any) -> SimpleNamespace:
    values = {
        "turnover_rate_percentile": 0.7,
        "amount_pct_of_market": 1.5,
        "holder_count_change": -1.0,
        "per_capita_holding_change": 2.0,
        "analyst_coverage_rank": 5,
        "hot_rank": 10,
    }
    values.update(overrides)
    return SimpleNamespace(values=values)


def _score_result() -> SimpleNamespace:
    return SimpleNamespace(
        scores=SimpleNamespace(total_score=62.5),
        position=SimpleNamespace(regime="震荡阶段", position_low=0.4, position_high=0.6),
        snapshot=SimpleNamespace(regime="震荡阶段"),
        missing_facts=["breadth_above_ma60_pct"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 七维度映射 + 嵌套结构提取
# ─────────────────────────────────────────────────────────────────────────────


def test_build_fundamental_factor_maps_nested_tables():
    factor, missing = build_fundamental_factor(_fin())
    assert factor.revenue_yoy == 10.5
    assert factor.roe == 15.2
    assert factor.gross_margin == 40.0
    assert factor.net_margin == 12.0
    assert factor.operating_cashflow == 2e8  # cashflow 表首条
    assert factor.goodwill == 5e7  # balance 表首条
    assert factor.free_cashflow == 1e8  # fina 表首条
    assert factor.debt_to_assets == 45.0
    assert factor.current_ratio == 1.8
    assert missing == []


def test_build_fundamental_factor_missing_is_none_not_zero():
    factor, missing = build_fundamental_factor(None)
    # 全部 None，绝不回退 0
    for name in ("revenue_yoy", "roe", "operating_cashflow", "goodwill", "debt_to_assets"):
        assert getattr(factor, name) is None
    assert len(missing) == 11
    assert "revenue_yoy" in missing and "operating_cashflow" in missing


def test_build_expectation_factor_forecast_express_and_consensus():
    exp = {
        "forecast": [{"profit_forecast_min_change": 5.0, "profit_forecast_max_change": 30.0}],
        "express": [{"express_revenue_yoy": 12.0, "express_net_profit_yoy": 15.0}],
    }
    factor, missing = build_expectation_factor(exp, _consensus())
    assert factor.profit_forecast_min_change == 5.0
    assert factor.profit_forecast_max_change == 30.0
    assert factor.express_revenue_yoy == 12.0
    assert factor.express_net_profit_yoy == 15.0
    assert factor.eps_fy1 == 3.2
    assert factor.rating_mean == 2.5
    assert factor.coverage_count == 20
    assert factor.rating_upgrade_1m == 3
    assert factor.rating_downgrade_1m == 1
    assert missing == []


def test_build_expectation_factor_consensus_none_registers_missing():
    exp = {"forecast": [], "express": []}
    factor, missing = build_expectation_factor(exp, None)
    assert factor.eps_fy1 is None
    assert "eps_fy1" in missing and "profit_forecast_min_change" in missing


def test_build_trend_factor_latest_daily_and_ma():
    factor, missing = build_trend_factor(_market(), _industry())
    assert factor.price_change_pct == 2.5  # latest_daily 嵌套
    assert factor.ma20 == 100.0 and factor.ma60 == 95.0 and factor.ma120 == 90.0  # ma 嵌套
    assert factor.rs_stock_market_20d == 3.0
    assert factor.rs_stock_industry_20d == 1.0
    assert factor.rs_industry_market_20d == 4.0
    assert factor.industry_breadth_20 == 60.0
    assert missing == []


def test_build_valuation_factor_latest_daily_basic_and_sw_daily():
    factor, missing = build_valuation_factor(_market(), _industry())
    assert factor.pe_ttm == 20.0  # latest_daily_basic 嵌套
    assert factor.pb_ratio == 3.0
    assert factor.pe_ttm_5y_avg == 25.0
    assert factor.pe_ttm_5y_percentile == 0.3
    assert factor.pb_5y_percentile == 0.4
    assert factor.industry_pe_ttm == 18.0  # sw_daily 首条
    assert factor.industry_pb == 2.0
    # peer 估值本批无数据源 → None + 登记
    assert factor.peer_median_pe_ttm is None and factor.peer_median_pb is None
    assert set(missing) == {"peer_median_pe_ttm", "peer_median_pb"}
    assert factor.percentile is None  # score_engine 输出，不在本模块计算


def test_build_crowding_factor_turnover_and_p0_values():
    factor, missing = build_crowding_factor(_market(), _crowding())
    assert factor.turnover_rate == 3.5  # latest_daily_basic 嵌套
    assert factor.turnover_rate_percentile == 0.7
    assert factor.holder_count_change == -1.0
    assert factor.hot_rank == 10
    assert factor.analyst_coverage_rank == 5
    # 无数据源字段 → None + 登记
    assert factor.institutional_holding_ratio is None
    assert factor.margin_balance_yoy is None
    assert factor.news_heat is None
    assert factor.leader_concentration is None
    assert set(missing) == {
        "institutional_holding_ratio",
        "margin_balance_yoy",
        "news_heat",
        "leader_concentration",
    }


def test_build_risk_factor_audit_list_nested():
    risk = {
        "pledge": [{"pledge_ratio": 12.0}],
        "repurchase": [{"repurchase_progress": "完成"}],
        "news": [{"news_title": "最新公告"}],
        "audit": [
            {
                "audit_opinion": "标准无保留意见",
                "audit_agency": "某会计师事务所",
                "audit_fees": 100000.0,
                "audit_sign": "张三",
            }
        ],
        "risk_missing": [],
    }
    factor, missing = build_risk_factor(risk, _fin())
    assert factor.pledge_ratio == 12.0
    assert factor.repurchase_progress == "完成"
    assert factor.news_title == "最新公告"
    assert factor.debt_to_assets == 45.0
    assert factor.goodwill == 5e7
    assert factor.audit is not None
    assert factor.audit.audit_opinion == "标准无保留意见"
    assert factor.audit.audit_fees == 100000.0
    assert missing == []


def test_build_risk_factor_audit_missing_registers_audit_opinion():
    risk = {"pledge": [], "repurchase": [], "news": [], "audit": [], "risk_missing": ["audit_opinion"]}
    factor, missing = build_risk_factor(risk, None)
    assert factor.audit is None
    assert factor.industry_risk is None
    assert "audit_opinion" in missing
    assert "pledge_ratio" in missing and "goodwill" in missing


def test_build_market_factor_from_regime_result():
    values = {
        "hs300_pe_ttm": 13.0,
        "hs300_pb": 1.4,
        "hs300_close": 4000.0,
        "market_turnover": 9000.0,
        "margin_balance": 18000.0,
    }
    factor, missing = build_market_factor(values, _score_result())
    assert factor.market_score == 62.5
    assert factor.regime == "震荡阶段"
    assert factor.position_low == 0.4 and factor.position_high == 0.6
    assert factor.hs300_pe_ttm == 13.0
    assert factor.breadth_above_ma60_pct is None  # 缺 breadth → None
    assert "breadth_above_ma60_pct" in missing  # 且登记（score_result.missing_facts 透传）


# ─────────────────────────────────────────────────────────────────────────────
# get_factor_snapshot 编排 + degraded + include_market
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def patch_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """把数据源函数替换为合成实现，避免任何真实网络/token 副作用。"""

    def _fin_fact(_s: str) -> dict[str, Any]:
        return _fin()

    def _exp_fact(_s: str) -> dict[str, Any]:
        return {"forecast": [], "express": []}

    def _mkt_fact(_s: str) -> dict[str, Any]:
        return _market()

    def _ind_fact(_s: str) -> dict[str, Any]:
        return _industry()

    def _risk_fact(_s: str) -> dict[str, Any]:
        return {"pledge": [], "repurchase": [], "news": [], "audit": [], "risk_missing": ["audit_opinion"]}

    def _regime() -> tuple[dict[str, Any], Any]:
        return (
            {
                "hs300_pe_ttm": 13.0,
                "hs300_pb": 1.4,
                "hs300_close": 4000.0,
                "market_turnover": 9000.0,
                "margin_balance": 18000.0,
            },
            _score_result(),
        )

    monkeypatch.setattr(factors_mod, "_get_financial_fact", _fin_fact)
    monkeypatch.setattr(factors_mod, "_get_expectation_fact", _exp_fact)
    monkeypatch.setattr(factors_mod, "_get_market_fact", _mkt_fact)
    monkeypatch.setattr(factors_mod, "_get_industry_fact", _ind_fact)
    monkeypatch.setattr(factors_mod, "_get_risk_fact", _risk_fact)
    monkeypatch.setattr(factors_mod, "_build_consensus", lambda _c: _consensus())
    monkeypatch.setattr(factors_mod, "_build_crowding", lambda _c: _crowding())
    monkeypatch.setattr(factors_mod, "_collect_market_regime", _regime)


def test_get_factor_snapshot_orchestrates_seven_factors(patch_sources: None):
    snap = get_factor_snapshot("300750.SZ")
    assert isinstance(snap, FactorSnapshot)
    assert snap.symbol == "300750.SZ"
    assert snap.as_of_date  # YYYY-MM-DD 非空
    assert snap.schema_version == "1"
    assert snap.fundamental.roe == 15.2
    assert snap.expectation.eps_fy1 == 3.2
    assert snap.trend.ma20 == 100.0
    assert snap.valuation.pe_ttm == 20.0
    assert snap.crowding.hot_rank == 10
    assert snap.risk.pledge_ratio is None  # pledge 缺失
    assert snap.market.market_score == 62.5
    # 缺失字段统一登记
    assert "pledge_ratio" in snap.missing_facts
    assert "audit_opinion" in snap.missing_facts
    assert snap.degraded is False


def test_get_factor_snapshot_symbol_normalization(patch_sources: None):
    # 6 位代码与带后缀都应归一化为 ts_code（.SH/.SZ），collector 入参为 6 位
    assert get_factor_snapshot("600519").symbol == "600519.SH"
    assert get_factor_snapshot("sz000001").symbol == "000001.SZ"
    assert factors_mod._to_pure_code("300750.SZ") == "300750"


def test_get_factor_snapshot_degraded_on_source_failure(patch_sources: None, monkeypatch: pytest.MonkeyPatch):
    def _boom(_s: str) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr(factors_mod, "_get_financial_fact", _boom)
    snap = get_factor_snapshot("300750.SZ")
    assert snap.degraded is True
    assert "fundamental:" in snap.degraded_reason
    # 该维度全 None，但其他维度照常映射
    assert snap.fundamental.roe is None
    assert snap.valuation.pe_ttm == 20.0
    assert "roe" in snap.missing_facts


def test_get_factor_snapshot_include_market_false(patch_sources: None, monkeypatch: pytest.MonkeyPatch):
    called: list[bool] = []

    def _regime() -> tuple[dict[str, Any], Any]:
        called.append(True)
        return ({}, _score_result())

    monkeypatch.setattr(factors_mod, "_collect_market_regime", _regime)
    snap = get_factor_snapshot("300750.SZ", include_market=False)
    assert called == []  # M 未被采集
    assert snap.market.market_score is None
    assert "market_score" not in snap.missing_facts  # 显式跳过不登记


# ─────────────────────────────────────────────────────────────────────────────
# 字段治理 + round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_no_external_field_leakage_in_source():
    """factors.py 源码不得出现外部数据源字段名（只允许 canonical 名）。"""
    src = factors_mod.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    forbidden = (
        "predictThisYearEps",
        "predictNextYearEps",
        "predictNextTwoYearEps",
        "indvAimPriceT",
        "emRatingValue",
        "lastEmRatingValue",
        "ratingChange",
        "本期股东人数",
        "人均持股数量增幅",
        "n_cashflow_act",
        "or_yoy",
        "netprofit_yoy",
        "grossprofit_margin",
        '"sc"',
        '"rk"',
        "total_revenue",
        "n_income",
        "total_liab",
    )
    for token in forbidden:
        assert token not in text, f"外部字段名泄漏到 factors.py: {token}"


def test_factor_snapshot_round_trip(patch_sources: None):
    snap = get_factor_snapshot("300750.SZ")
    data = snap.model_dump()
    restored = FactorSnapshot.model_validate(data)
    assert restored.symbol == snap.symbol
    assert restored.fundamental.roe == snap.fundamental.roe
    assert restored.expectation.eps_fy1 == snap.expectation.eps_fy1
    assert restored.trend.ma20 == snap.trend.ma20
    assert restored.valuation.pe_ttm == snap.valuation.pe_ttm
    assert restored.crowding.hot_rank == snap.crowding.hot_rank
    assert restored.market.market_score == snap.market.market_score
    assert restored.missing_facts == snap.missing_facts
