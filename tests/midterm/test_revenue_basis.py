"""边际营收口径（P1-4）单元测试：营业收入优先、缺失回落营业总收入。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §10：

- L 的 ``revenue_yoy`` 是 **营业收入**同比（``fina.or_yoy``）；边际的 ``q_revenue_yoy``
  必须同口径才能相减成「加速度」。旧实现用 ``income.revenue``（营业总收入）会引入
  ±0.1~0.2pp 口径噪声（600519 Q1：6.538 vs 6.336）。
- ``q_gross_margin`` 的分子（毛利）与 ``grossprofit_margin`` 的分母都是营业收入，
  因此营收序列同样优先 ``operating_revenue``；仅在缺失时才回落 ``revenue``，并在
  ``FundamentalFactor.marginal_revenue_basis`` 记录实际口径。

全部合成数据，无网络、无数据源副作用。
"""

from __future__ import annotations

from typing import Any

import pytest

from alphabee.midterm.factors import (
    _Q_MARGINAL_FIELDS,
    _marginal_revenue_series,
    build_fundamental_factor,
    derive_marginal,
)


def _fin(income: list[dict[str, Any]], fina: list[dict[str, Any]]) -> dict[str, Any]:
    return {"income": income, "fina": fina}


def _cashflow() -> dict[str, Any]:
    return {"cashflow": []}


# ─────────────────────────────────────────────────────────────────────────────
# 600519 / 000333 Q1：q_revenue_yoy == or_yoy（营业收入口径）
# ─────────────────────────────────────────────────────────────────────────────


def test_600519_q1_operating_revenue_matches_or_yoy():
    """600519 Q1：营业收入序列派生 q_revenue_yoy == or_yoy 6.538（±1e-6）。

    夹具刻意让营业总收入口径给出不同结果（108.5/101 ⇒ 7.4257%），证明实现确实
    用了 ``operating_revenue`` 而不是 ``revenue``。
    """
    fin = _fin(
        income=[
            {"period": "20240331", "operating_revenue": 100.0, "revenue": 101.0, "net_profit_attr_p": 50.0},
            {"period": "20250331", "operating_revenue": 106.538, "revenue": 108.5, "net_profit_attr_p": 55.0},
        ],
        fina=[
            {"period": "20240331", "gross_margin": 90.0},
            {"period": "20250331", "gross_margin": 91.0},
        ],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] == pytest.approx(6.538, abs=1e-6)
    # 若误用营业总收入（108.5 / 101）⇒ 7.4257%，与设计实测口径不符
    assert result["q_revenue_yoy"] != pytest.approx(7.4257, abs=1e-3)
    assert set(result) == set(_Q_MARGINAL_FIELDS)  # 仍只返回 4 个 q_*


def test_000333_q1_operating_revenue_matches_or_yoy():
    """000333 Q1：营业收入序列派生 2.5501（设计实测值，±1e-6）。"""
    fin = _fin(
        income=[
            {"period": "20240331", "operating_revenue": 100.0, "revenue": 105.0, "net_profit_attr_p": 30.0},
            {"period": "20250331", "operating_revenue": 102.5501, "revenue": 108.0, "net_profit_attr_p": 33.0},
        ],
        fina=[
            {"period": "20240331", "gross_margin": 25.0},
            {"period": "20250331", "gross_margin": 26.0},
        ],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] == pytest.approx(2.5501, abs=1e-6)
    assert result["q_revenue_yoy"] != pytest.approx((108.0 / 105.0 - 1.0) * 100.0, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# 缺失 operating_revenue：回落营业总收入并留痕 basis
# ─────────────────────────────────────────────────────────────────────────────


def _fallback_fin() -> dict[str, Any]:
    """无 operating_revenue 序列：营业总收入 100 → 110（同比 +10%）。"""
    return _fin(
        income=[
            {"period": "20240331", "revenue": 100.0, "net_profit_attr_p": 10.0},
            {"period": "20250331", "revenue": 110.0, "net_profit_attr_p": 12.0},
        ],
        fina=[
            {"period": "20240331", "gross_margin": 30.0},
            {"period": "20250331", "gross_margin": 32.0},
        ],
    )


def test_missing_operating_revenue_falls_back_to_total_revenue():
    """缺 operating_revenue ⇒ 用 revenue 计算 q_revenue_yoy，basis=total_revenue。"""
    fin = _fallback_fin()
    result = derive_marginal(fin)
    assert result["q_revenue_yoy"] == pytest.approx(10.0)

    factor, _missing = build_fundamental_factor({**fin, **_cashflow()})
    assert factor.marginal_revenue_basis == "total_revenue"


def test_operating_revenue_present_records_basis_operating_revenue():
    """有 operating_revenue ⇒ basis=operating_revenue（q 值来自营业收入序列）。"""
    fin = _fin(
        income=[
            {"period": "20240331", "operating_revenue": 100.0, "revenue": 100.0, "net_profit_attr_p": 10.0},
            {"period": "20250331", "operating_revenue": 120.0, "revenue": 110.0, "net_profit_attr_p": 12.0},
        ],
        fina=[
            {"period": "20240331", "gross_margin": 30.0},
            {"period": "20250331", "gross_margin": 32.0},
        ],
    )
    factor, _missing = build_fundamental_factor({**fin, **_cashflow()})
    assert factor.marginal_revenue_basis == "operating_revenue"
    assert factor.q_revenue_yoy == pytest.approx(20.0)  # 120/100 − 1（不是 110/100）


def test_no_revenue_series_records_empty_basis():
    """income 无任何有效营收序列 ⇒ q_* 为 None、basis 为空串（不猜测口径）。"""
    fin = _fin(income=[], fina=[{"period": "20250331", "gross_margin": 30.0}])
    factor, missing = build_fundamental_factor({**fin, **_cashflow()})

    assert factor.marginal_revenue_basis == ""
    assert factor.q_revenue_yoy is None
    assert "marginal_revenue_basis" not in missing  # provenance 非 canonical，不登记缺失


# ─────────────────────────────────────────────────────────────────────────────
# _marginal_revenue_series：优先级与回落
# ─────────────────────────────────────────────────────────────────────────────


def test_marginal_revenue_series_prefers_operating_revenue():
    income = [
        {"period": "20250930", "operating_revenue": 300.0, "revenue": 500.0},
        {"period": "20250630", "operating_revenue": 200.0, "revenue": 400.0},
    ]
    series, basis = _marginal_revenue_series(income)
    assert basis == "operating_revenue"
    assert series == {"20250930": 300.0, "20250630": 200.0}


def test_marginal_revenue_series_falls_back_when_operating_missing_or_invalid():
    income = [
        {"period": "20250930", "operating_revenue": None, "revenue": 500.0},
        {"period": "20250630", "revenue": 400.0},
    ]
    series, basis = _marginal_revenue_series(income)
    assert basis == "total_revenue"
    assert series == {"20250930": 500.0, "20250630": 400.0}

    empty_series, empty_basis = _marginal_revenue_series(None)
    assert empty_series == {}
    assert empty_basis == "total_revenue"  # 无序列时 basis 由 builder 归空串


# ─────────────────────────────────────────────────────────────────────────────
# q_gross_margin：营业收入 × 毛利率（分子分母同源）
# ─────────────────────────────────────────────────────────────────────────────


def test_q_gross_margin_uses_operating_revenue_basis():
    """q_gross_margin 按「营业收入 × 毛利率」差分：300×30% − 200×20% = 50（单季营收 100）。

    若误用营业总收入（500/400）会得到 70%——本用例钉住分子分母同源。
    """
    fin = _fin(
        income=[
            {"period": "20240630", "operating_revenue": 150.0, "revenue": 300.0, "net_profit_attr_p": 15.0},
            {"period": "20240930", "operating_revenue": 250.0, "revenue": 400.0, "net_profit_attr_p": 25.0},
            {"period": "20250630", "operating_revenue": 200.0, "revenue": 400.0, "net_profit_attr_p": 20.0},
            {"period": "20250930", "operating_revenue": 300.0, "revenue": 500.0, "net_profit_attr_p": 40.0},
        ],
        fina=[
            {"period": "20240630", "gross_margin": 18.0},
            {"period": "20240930", "gross_margin": 20.0},
            {"period": "20250630", "gross_margin": 20.0},
            {"period": "20250930", "gross_margin": 30.0},
        ],
    )
    result = derive_marginal(fin)

    assert result["q_gross_margin"] == pytest.approx(50.0)
    assert result["q_gross_margin"] != pytest.approx(70.0, abs=1e-3)  # 总营收口径的反例
    # 单季营收 100 vs 去年同季 100 ⇒ 同比 0%（同样来自营业收入序列）
    assert result["q_revenue_yoy"] == pytest.approx(0.0)
