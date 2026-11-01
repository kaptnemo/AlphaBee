"""F「边际改善」数据层（单季还原）的单元测试：设计 §8.1 的 D1–D9。

口径依据 ``docs/implementation/midterm/MIDTERM_F_MARGINAL_DESIGN.md`` §3.1–§3.5：

- 单季值 = 累计(t) − 累计(t_prev)，Q1（0331）取累计；``t_prev`` 由**报告期字符串**
  决定（0630→0331、0930→0630、1231→0930），不取数组下标；
- 单季同比 = 单季(t)/单季(t−4) − 1（PERCENT）；单季毛利率 = 单季毛利/单季营收，
  其中单季毛利 = ``累计营收 × 累计毛利率`` 差分（§3.5 口径近似）；
- 缺期/数据异常 ⇒ 对应字段 ``None``（**不插值、不用 TTM 顶替**）。

全部用合成数据，无网络、无数据源副作用（只导入 ``factors`` 的纯函数）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from alphabee.midterm.factors import (
    _Q_MARGINAL_FIELDS,
    _normalize_period,
    _prev_quarter,
    _prev_year,
    _series,
    _single_quarter,
    derive_marginal,
)
from alphabee.midterm.models import FundamentalFactor

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "alphabee" / "schemas" / "financial.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# 合成数据构造（只用 §3.1 的三列：revenue / net_profit_attr_p / gross_margin）
# ─────────────────────────────────────────────────────────────────────────────


def _income(period: str, revenue: float | None, net_profit_attr_p: float | None) -> dict[str, Any]:
    return {"period": period, "revenue": revenue, "net_profit_attr_p": net_profit_attr_p}


def _fina(period: str, gross_margin: float | None) -> dict[str, Any]:
    return {"period": period, "gross_margin": gross_margin}


def _fin_data(income: list[dict[str, Any]], fina: list[dict[str, Any]]) -> dict[str, Any]:
    return {"income": income, "fina": fina}


def _q3_dataset() -> dict[str, Any]:
    """D1/D4 基准：t = 2025Q3（20250930），含去年同季与各自 t_prev。

    累计营收：20240630=250 / 20240930=450 / 20250630=330 / 20250930=600
      单季营收 2025Q3 = 600−330 = 270；2024Q3 = 450−250 = 200 ⇒ 同比 +35.0%
    累计归母净利：20240630=30 / 20240930=45 / 20250630=20 / 20250930=40
      单季净利 2025Q3 = 20；2024Q3 = 15 ⇒ 同比 +33.33%
    累计毛利率(%)：20240630=20 / 20240930=22 / 20250630=18 / 20250930=19
      单季毛利 2025Q3 = 600×0.19 − 330×0.18 = 54.6 ⇒ 毛利率 54.6/270 = 20.222%
      单季毛利 2024Q3 = 450×0.22 − 250×0.20 = 49.0 ⇒ 毛利率 49.0/200 = 24.5%
      ⇒ 毛利率同比变化 = 20.222 − 24.5 = −4.278pp
    """
    return _fin_data(
        income=[
            _income("20250630", 330.0, 20.0),
            _income("20250930", 600.0, 40.0),
            _income("20240630", 250.0, 30.0),
            _income("20240930", 450.0, 45.0),
        ],
        fina=[
            _fina("20250930", 19.0),
            _fina("20250630", 18.0),
            _fina("20240930", 22.0),
            _fina("20240630", 20.0),
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# D1 Q3 正常路径 / D2 Q1 路径 / D3 年报路径
# ─────────────────────────────────────────────────────────────────────────────


def test_d1_q3_normal_path():
    """D1：单季营收 = 9M − H1；单季同比 = 单季/去年 Q3 − 1。"""
    result = derive_marginal(_q3_dataset())

    assert result["q_revenue_yoy"] == pytest.approx(((600.0 - 330.0) / (450.0 - 250.0) - 1.0) * 100.0)
    assert result["q_revenue_yoy"] == pytest.approx(35.0)
    assert result["q_net_profit_yoy"] == pytest.approx((20.0 / 15.0 - 1.0) * 100.0)
    assert result["q_gross_margin"] == pytest.approx(54.6 / 270.0 * 100.0)
    assert result["q_gross_margin_pp"] == pytest.approx(54.6 / 270.0 * 100.0 - 49.0 / 200.0 * 100.0)


def test_d2_q1_uses_cumulative_as_single_quarter():
    """D2：Q1 单季 = 累计（不调用 t_prev，年份边界）。"""
    assert _prev_quarter("20240331") is None  # Q1 无同年上一期
    fin = _fin_data(
        income=[_income("20230331", 80.0, 10.0), _income("20240331", 100.0, 12.0)],
        fina=[_fina("20230331", 30.0), _fina("20240331", 32.0)],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] == pytest.approx((100.0 / 80.0 - 1.0) * 100.0)
    assert result["q_net_profit_yoy"] == pytest.approx((12.0 / 10.0 - 1.0) * 100.0)
    # Q1 单季毛利 = 累计毛利 ⇒ 单季毛利率恒等于累计毛利率
    assert result["q_gross_margin"] == pytest.approx(32.0)
    assert result["q_gross_margin_pp"] == pytest.approx(32.0 - 30.0)


def test_d3_annual_report_subtracts_same_year_q3():
    """D3：年报 t_prev = 同年 0930（不是同年 H1，也不是 t−1 期下标）。"""
    assert _prev_quarter("20241231") == "20240930"
    fin = _fin_data(
        income=[
            _income("20230930", 300.0, 30.0),
            _income("20231231", 500.0, 50.0),
            _income("20240630", 250.0, 28.0),  # 干扰项：若误把 H1 当 t_prev，Q4 单季会变 450
            _income("20240930", 450.0, 45.0),
            _income("20241231", 700.0, 70.0),
        ],
        fina=[
            _fina("20230930", 20.0),
            _fina("20231231", 21.0),
            _fina("20240630", 20.0),
            _fina("20240930", 22.0),
            _fina("20241231", 24.0),
        ],
    )
    result = derive_marginal(fin)

    # 2024Q4 单季营收 = 700 − 450 = 250；2023Q4 = 500 − 300 = 200 ⇒ 25.0%
    assert result["q_revenue_yoy"] == pytest.approx(25.0)
    # 2024Q4 单季净利 = 70 − 45 = 25；2023Q4 = 50 − 30 = 20 ⇒ 25.0%
    assert result["q_net_profit_yoy"] == pytest.approx(25.0)
    # 2024Q4 单季毛利 = 700×0.24 − 450×0.22 = 69 ⇒ 27.6%；2023Q4 = 500×0.21 − 300×0.20 = 45 ⇒ 22.5%
    assert result["q_gross_margin"] == pytest.approx(69.0 / 250.0 * 100.0)
    assert result["q_gross_margin_pp"] == pytest.approx(69.0 / 250.0 * 100.0 - 45.0 / 200.0 * 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# D4 数组乱序（先按 period 降序排序再去重）
# ─────────────────────────────────────────────────────────────────────────────


def test_d4_array_order_independent():
    """D4：输入倒序/乱序 ⇒ 结果逐值一致（一律先排序）。"""
    base = derive_marginal(_q3_dataset())

    reversed_data = _q3_dataset()
    reversed_data["income"] = list(reversed(reversed_data["income"]))
    reversed_data["fina"] = list(reversed(reversed_data["fina"]))
    assert derive_marginal(reversed_data) == base

    interleaved = _fin_data(
        income=[
            _income("20240930", 450.0, 45.0),
            _income("20250930", 600.0, 40.0),
            _income("20240630", 250.0, 30.0),
            _income("20250630", 330.0, 20.0),
        ],
        fina=[
            _fina("20250630", 18.0),
            _fina("20240630", 20.0),
            _fina("20250930", 19.0),
            _fina("20240930", 22.0),
        ],
    )
    assert derive_marginal(interleaved) == base


def test_d4_series_sorts_desc_and_first_record_wins():
    """D4：``_series`` 降序排序 + 同报告期去重（排序稳定 ⇒ 首条生效，后条不覆盖）。"""
    series = _series(
        [
            {"period": "20240630", "revenue": 250.0},
            {"period": "20250930", "revenue": 600.0},
            {"period": "20250930", "revenue": 999.0},  # 同报告期重复记录 ⇒ 丢弃
        ],
        "revenue",
    )
    assert list(series) == ["20250930", "20240630"]  # 降序
    assert series["20250930"] == 600.0


# ─────────────────────────────────────────────────────────────────────────────
# D5 期数不足（不插值、不用 TTM 顶替）
# ─────────────────────────────────────────────────────────────────────────────


def test_d5_missing_t_minus_4_yields_none_yoy_only():
    """D5：缺去年同季 ⇒ 两个同比 None；当期单季毛利率仍可算。"""
    fin = _fin_data(
        income=[_income("20250630", 330.0, 20.0), _income("20250930", 600.0, 40.0)],
        fina=[_fina("20250630", 18.0), _fina("20250930", 19.0)],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] is None
    assert result["q_net_profit_yoy"] is None
    assert result["q_gross_margin"] == pytest.approx(54.6 / 270.0 * 100.0)
    assert result["q_gross_margin_pp"] is None  # 缺 t−4 的单季毛利率


def test_d5_missing_t_prev_yields_none_single_quarter():
    """D5：非 Q1 缺 t_prev ⇒ 单季值不可还原 ⇒ 全部 None（不插值）。"""
    fin = _fin_data(
        income=[_income("20250930", 600.0, 40.0)],
        fina=[_fina("20250930", 19.0)],
    )
    result = derive_marginal(fin)
    assert all(value is None for value in result.values())


def test_d5_no_interpolation_when_intermediate_period_missing():
    """D5：缺 0630 时不得用 0930−0331 顶替 Q3 单季（不插值）。"""
    fin = _fin_data(
        income=[
            _income("20250331", 150.0, 8.0),
            _income("20250930", 600.0, 40.0),
            _income("20240630", 250.0, 30.0),
            _income("20240930", 450.0, 45.0),
        ],
        fina=[
            _fina("20250331", 17.0),
            _fina("20250930", 19.0),
            _fina("20240630", 20.0),
            _fina("20240930", 22.0),
        ],
    )
    result = derive_marginal(fin)
    assert result["q_revenue_yoy"] is None
    assert result["q_gross_margin"] is None


# ─────────────────────────────────────────────────────────────────────────────
# D6 上年同期 ≤ 0 / D7 分母过小 / D8 异常毛利率
# ─────────────────────────────────────────────────────────────────────────────


def test_d6_prior_year_net_profit_non_positive_keeps_revenue_leg():
    """D6：上年同期单季净利 ≤ 0（亏损/转盈）⇒ 净利同比 None；营收腿仍可用（§4.5）。"""
    fin = _fin_data(
        income=[
            _income("20240630", 250.0, 30.0),
            _income("20240930", 450.0, 25.0),  # 2024Q3 单季净利 = 25 − 30 = −5
            _income("20250630", 330.0, 20.0),
            _income("20250930", 600.0, 40.0),
        ],
        fina=[
            _fina("20240630", 20.0),
            _fina("20240930", 22.0),
            _fina("20250630", 18.0),
            _fina("20250930", 19.0),
        ],
    )
    result = derive_marginal(fin)

    assert result["q_net_profit_yoy"] is None  # 百分比无意义，不得给出虚假增速
    assert result["q_revenue_yoy"] == pytest.approx(35.0)  # 营收腿不受影响
    assert result["q_gross_margin"] is not None


def test_d7_small_prior_year_revenue_base_kills_revenue_leg():
    """D7：|单季营收(t−4)| < 1% × 当期累计营收 ⇒ 营收腿 None，净利腿不受影响。"""
    fin = _fin_data(
        income=[
            _income("20240630", 450.0, 30.0),
            _income("20240930", 455.0, 60.0),  # 2024Q3 单季营收 = 5 < 1% × 600 = 6
            _income("20250630", 330.0, 20.0),
            _income("20250930", 600.0, 40.0),  # 单季净利 20；2024Q3 单季净利 30 ≥ 6
        ],
        fina=[_fina("20240630", 20.0), _fina("20240930", 22.0), _fina("20250630", 18.0), _fina("20250930", 19.0)],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] is None
    assert result["q_net_profit_yoy"] == pytest.approx((20.0 / 30.0 - 1.0) * 100.0)


def test_d7_small_prior_year_net_profit_base_kills_net_leg():
    """D7：上年同季单季净利是正数但过小 ⇒ 净利腿 None，营收腿不受影响。"""
    fin = _fin_data(
        income=[
            _income("20240630", 250.0, 50.0),
            _income("20240930", 450.0, 53.0),  # 2024Q3 单季净利 = 3 < 1% × 600 = 6
            _income("20250630", 330.0, 20.0),
            _income("20250930", 600.0, 40.0),
        ],
        fina=[_fina("20240630", 20.0), _fina("20240930", 22.0), _fina("20250630", 18.0), _fina("20250930", 19.0)],
    )
    result = derive_marginal(fin)

    assert result["q_net_profit_yoy"] is None
    assert result["q_revenue_yoy"] == pytest.approx(35.0)


@pytest.mark.parametrize("reported_margin", [80.0, -20.0])
def test_d8_abnormal_single_quarter_gross_margin_returns_none(reported_margin: float):
    """D8：单季毛利率 ∉ [−50, 100] ⇒ None（不静默夹取），其他腿不受影响。"""
    fin = _fin_data(
        income=[
            _income("20240630", 250.0, 30.0),
            _income("20240930", 450.0, 45.0),
            _income("20250630", 330.0, 40.0),
            _income("20250930", 600.0, 40.0),
        ],
        fina=[
            _fina("20240630", 20.0),
            _fina("20240930", 22.0),
            _fina("20250630", 18.0),
            _fina("20250930", reported_margin),  # 单季毛利率被算到 >100% 或 <−50%
        ],
    )
    result = derive_marginal(fin)

    assert result["q_gross_margin"] is None
    assert result["q_gross_margin_pp"] is None
    assert result["q_revenue_yoy"] == pytest.approx(35.0)


def test_non_positive_single_quarter_revenue_kills_revenue_leg():
    """§4.5：单季营收为负/为 0 ⇒ 该腿 None（分母无意义），毛利率同时不可还原。"""
    fin = _fin_data(
        income=[
            _income("20240630", 250.0, 30.0),
            _income("20240930", 200.0, 45.0),  # 2024Q3 单季营收 = −50
            _income("20250630", 330.0, 20.0),
            _income("20250930", 300.0, 40.0),  # 2025Q3 单季营收 = −30 ≤ 0
        ],
        fina=[_fina("20240630", 20.0), _fina("20240930", 22.0), _fina("20250630", 18.0), _fina("20250930", 19.0)],
    )
    result = derive_marginal(fin)

    assert result["q_revenue_yoy"] is None
    assert result["q_gross_margin"] is None  # 单季营收 ≤ 0 ⇒ 毛利率分母无意义


# ─────────────────────────────────────────────────────────────────────────────
# D9 全空 fin_data
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fin_data",
    [
        None,
        {},
        {"income": [], "fina": []},
        {"income": None, "fina": None},
        {"income": [{}], "fina": []},
        {"income": [{"period": "bad", "revenue": 1.0}], "fina": []},
    ],
)
def test_d9_empty_fin_data_returns_all_none(fin_data: dict[str, Any] | None):
    """D9：全空/畸形 fin_data ⇒ 4 字段全 None，不抛异常。"""
    result = derive_marginal(fin_data)
    assert set(result) == set(_Q_MARGINAL_FIELDS)
    assert all(value is None for value in result.values())


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数契约（§3.4）：归一 / 期运算 / 单季还原 / 无副作用
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20250930", "20250930"),
        ("2025-09-30", "20250930"),
        (20250930, "20250930"),
        (" 20250930 ", "20250930"),
        ("2025-9-3", None),
        ("2025093", None),
        ("", None),
        ("nan", None),
        (None, None),
        (True, None),
    ],
)
def test_normalize_period_contract(value: Any, expected: str | None):
    assert _normalize_period(value) == expected


def test_period_arithmetic_contract():
    assert _prev_year("20260930") == "20250930"
    assert _prev_quarter("20260930") == "20260630"  # 跨年由字符串决定，不取数组下标
    assert _prev_quarter("20260630") == "20260331"
    assert _prev_quarter("20261231") == "20260930"  # 年报 → 同年 0930
    assert _prev_quarter("20260331") is None  # Q1 无同年上一期


def test_single_quarter_contract():
    series = {"20250930": 600.0, "20250630": 330.0, "20250331": 150.0}
    assert _single_quarter(series, "20250930") == pytest.approx(270.0)  # 累计差分
    assert _single_quarter(series, "20250331") == pytest.approx(150.0)  # Q1 取累计
    assert _single_quarter(series, "20250630") == pytest.approx(180.0)
    assert _single_quarter(series, "20251231") is None  # 缺 t
    assert _single_quarter({"20250930": 600.0}, "20250930") is None  # 非 Q1 缺 t_prev
    assert _single_quarter({}, "20250930") is None


def test_derive_marginal_is_pure_and_does_not_mutate_input():
    """纯函数：两次调用逐值相同，且不改写/重排调用方的列表（无缓存副作用）。"""
    fin = _q3_dataset()
    snapshot = copy.deepcopy(fin)

    first = derive_marginal(fin)
    second = derive_marginal(fin)
    assert first == second
    assert fin == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# 字段治理：q_* 是「多期还原派生字段，非 canonical 单期事实」（§3.3）
# ─────────────────────────────────────────────────────────────────────────────


def test_q_fields_are_not_canonical_schema_fields():
    """4 个 q_* 字段不得写进 schemas/financial.yaml（计算产物 ≠ 数据源事实）。"""
    canonical = set(yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8")))
    assert set(_Q_MARGINAL_FIELDS).isdisjoint(canonical)
    # 输入三列必须是 canonical（§3.1 只读这三列）
    assert {"revenue", "net_profit_attr_p", "gross_margin"} <= canonical


def test_q_fields_default_to_none_and_are_documented_as_derived():
    factor = FundamentalFactor()
    for name in _Q_MARGINAL_FIELDS:
        assert getattr(factor, name) is None
    # 设计 §3.3 要求 docstring 写明派生字段治理口径（防止后续被当成 canonical 事实）
    assert "多期还原派生字段，非 canonical 单期事实" in (FundamentalFactor.__doc__ or "")


# ─────────────────────────────────────────────────────────────────────────────
# 边界定值（t3 评审补强）：分母门槛的严格不等式语义 + 单季毛利率端点语义
#
# 这两组用例把「恰等门槛/恰在端点 ⇒ 保留」钉死。若把实现改成 `>`（要求严格大于门槛）
# 或把区间判据改成 `low < x < high`（端点排除），对应用例必然变红——语义不能漂移。
# ─────────────────────────────────────────────────────────────────────────────


def _threshold_dataset(base_single_revenue: float, base_single_net_profit: float) -> dict[str, Any]:
    """1% 门槛用例：t=20250930 当期累计营收 500 ⇒ 门槛 = 5.0（严格 ``<`` 才判 None）。

    单季营收(t) = 500 − 400 = 100；去年同季单季营收 = ``base_single_revenue``。
    单季净利(t) = 60 − 40 = 20；去年同季单季净利 = ``base_single_net_profit``。
    """
    return _fin_data(
        income=[
            _income("20240630", 300.0, 50.0),
            _income("20240930", 300.0 + base_single_revenue, 50.0 + base_single_net_profit),
            _income("20250630", 400.0, 40.0),
            _income("20250930", 500.0, 60.0),
        ],
        fina=[
            _fina("20240630", 20.0),
            _fina("20240930", 20.0),
            _fina("20250630", 20.0),
            _fina("20250930", 20.0),
        ],
    )


@pytest.mark.parametrize(
    ("base_single_revenue", "expected"),
    [
        (5.00, 1900.0),  # 恰等门槛（1% × 500）⇒ 保留：严格不等式才是「过小」
        (4.99, None),  # 恰低于门槛 ⇒ None
        (5.01, (100.0 / 5.01 - 1.0) * 100.0),  # 恰高于门槛 ⇒ 保留
    ],
)
def test_small_base_threshold_uses_strict_inequality(base_single_revenue: float, expected: float | None):
    result = derive_marginal(_threshold_dataset(base_single_revenue, base_single_net_profit=100.0))
    if expected is None:
        assert result["q_revenue_yoy"] is None
    else:
        assert result["q_revenue_yoy"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("base_single_net_profit", "expected"),
    [
        (5.00, 300.0),  # 单季净利 20 / 去年同季 5 ⇒ +300%
        (4.99, None),
        (5.01, (20.0 / 5.01 - 1.0) * 100.0),
    ],
)
def test_small_base_threshold_applies_to_net_profit_leg(base_single_net_profit: float, expected: float | None):
    result = derive_marginal(
        _threshold_dataset(base_single_revenue=100.0, base_single_net_profit=base_single_net_profit)
    )
    if expected is None:
        assert result["q_net_profit_yoy"] is None
        assert result["q_revenue_yoy"] is not None  # 营收腿不受净利腿门槛影响
    else:
        assert result["q_net_profit_yoy"] == pytest.approx(expected)


def _margin_endpoint_dataset(current_gross_margin: float) -> dict[str, Any]:
    """毛利率端点用例：单季营收 100、上一期累计毛利 400×0.20=80。

    单季毛利率 = (500×gm − 80) / 100 × 100；因此 gm=36 ⇒ 100%、gm=6 ⇒ −50%。
    """
    return _fin_data(
        income=[
            _income("20250630", 400.0, 40.0),
            _income("20250930", 500.0, 60.0),
        ],
        fina=[
            _fina("20250630", 20.0),
            _fina("20250930", current_gross_margin),
        ],
    )


@pytest.mark.parametrize(
    ("current_gross_margin", "expected"),
    [
        (36.0, 100.0),  # 恰在上界 ⇒ 保留（闭区间）
        (36.00002, None),  # 略高于上界 ⇒ None（不夹取）
        (6.0, -50.0),  # 恰在下界 ⇒ 保留
        (5.99998, None),  # 略低于下界 ⇒ None
    ],
)
def test_single_quarter_gross_margin_endpoints_are_inclusive(current_gross_margin: float, expected: float | None):
    result = derive_marginal(_margin_endpoint_dataset(current_gross_margin))
    if expected is None:
        assert result["q_gross_margin"] is None
        assert result["q_gross_margin_pp"] is None
    else:
        assert result["q_gross_margin"] == pytest.approx(expected)
