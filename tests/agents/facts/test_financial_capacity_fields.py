"""产能与扣非字段的接入链路：adapter 映射 → 提取层 → 快照模型 → 渲染。

背景：``在建工程 / 固定资产 / 扣非归母净利润`` 此前只存在于半年报文本与画像缓存里，
tushare 有数但 adapter 未映射，导致指标卡在三季报发布后仍引用 2026H1 的旧值。
本测试锁住"数据进得来、还能被渲染出来"这两段。
"""

from __future__ import annotations

import pandas as pd
import pytest

from alphabee.adapters.tushare import TuShare_Adapter
from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot
from alphabee.agents.facts.tools.financial_fact import extract_financial_facts, render


def _raw_data() -> dict:
    """最小可用的多期财务字典（字段已在上游被 adapter 转成 canonical 名）。

    键必须覆盖 ``render()`` 读取的全部列，否则渲染路径会 KeyError。
    """
    periods = ["20260930", "20260630", "20251231"]
    revenues = [45_537_430_834.54, 28_897_478_718.36, 40_000_000_000.00]
    cips = [9_532_794_486.59, 8_247_000_000.00, 5_875_518_226.97]
    fixed = [18_927_930_698.16, 19_400_000_000.00, 20_000_000_000.00]
    dedt = [16_476_713_443.03, 10_572_173_661.42, 14_000_000_000.00]
    da = [2_600_000_000.00, 1_760_000_000.00, 3_000_000_000.00]

    def _income(i: int) -> dict:
        return {
            "period": periods[i],
            "revenue": revenues[i],
            "operating_profit": revenues[i] * 0.5,
            "net_profit": revenues[i] * 0.4,
            "net_profit_attr_p": revenues[i] * 0.39,
            "ebitda": revenues[i] * 0.55,
            "basic_eps": 1.23,
        }

    def _balance(i: int) -> dict:
        return {
            "period": periods[i],
            "total_assets": 1.25e11,
            "total_liabilities": 2.7e10,
            "shareholders_equity": 9.6e10,
            "cash": 3.7e10,
            "current_assets": 6.0e10,
            "current_liabilities": 1.8e10,
            "construction_in_progress": cips[i],
            "fixed_assets": fixed[i],
        }

    def _cashflow(i: int) -> dict:
        return {
            "period": periods[i],
            "operating_cashflow": 1.7e10,
            "investing_cashflow": -1.1e10,
            "financing_cashflow": 3.0e9,
            "capex": 4.0e9,
        }

    def _fina(i: int) -> dict:
        return {
            "period": periods[i],
            "roe": 21.97,
            "roa": 15.0,
            "gross_margin": 54.2,
            "net_margin": 42.83,
            "current_ratio": 3.429,
            "quick_ratio": 2.8,
            "debt_to_assets": 22.13,
            "revenue_yoy": 38.59,
            "net_profit_yoy": 60.64,
            "eps_growth_yoy": 56.71,
            "net_profit_dedt": dedt[i],
            "depreciation_amortization": da[i],
        }

    return {
        "stock_code": "603259.SH",
        "ref_dates": periods,
        "income": [_income(i) for i in range(3)],
        "balance": [_balance(i) for i in range(3)],
        "cashflow": [_cashflow(i) for i in range(3)],
        "fina": [_fina(i) for i in range(3)],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Adapter 映射
# ═══════════════════════════════════════════════════════════════════════════


class TestAdapterMapping:
    def test_balancesheet_maps_cip_and_fix_assets(self):
        raw = pd.DataFrame({"end_date": ["20260930"], "cip": [9.5e9], "fix_assets": [1.89e10]})
        adapted = TuShare_Adapter.adapt("balancesheet", raw)
        assert "construction_in_progress" in adapted.columns
        assert "fixed_assets" in adapted.columns
        assert adapted.loc[0, "construction_in_progress"] == pytest.approx(9.5e9)

    def test_fina_indicator_maps_profit_dedt(self):
        raw = pd.DataFrame({"end_date": ["20260930"], "profit_dedt": [1.6477e10]})
        adapted = TuShare_Adapter.adapt("fina_indicator", raw)
        assert "net_profit_dedt" in adapted.columns
        assert adapted.loc[0, "net_profit_dedt"] == pytest.approx(1.6477e10)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Canonical 提取层
# ═══════════════════════════════════════════════════════════════════════════


class TestFieldExtraction:
    def test_extracts_latest_period_values(self):
        values = extract_financial_facts(_raw_data())
        assert values["construction_in_progress"] == pytest.approx(9_532_794_486.59)
        assert values["fixed_assets"] == pytest.approx(18_927_930_698.16)
        assert values["net_profit_dedt"] == pytest.approx(16_476_713_443.03)

    def test_missing_period_yields_no_key(self):
        data = _raw_data()
        for row in data["balance"]:
            row.pop("construction_in_progress", None)
        values = extract_financial_facts(data)
        assert "construction_in_progress" not in values


# ═══════════════════════════════════════════════════════════════════════════
# 3. 快照模型 → fact_values
# ═══════════════════════════════════════════════════════════════════════════


class TestSnapshotToFactValues:
    def test_new_fields_are_exported(self):
        facts = FinancialFacts(
            stock_code="603259.SH",
            snapshots=[
                FinancialSnapshot(
                    period="20260930",
                    revenue=4.5537e10,
                    construction_in_progress=9.5328e9,
                    fixed_assets=1.8928e10,
                    net_profit_dedt=1.6477e10,
                )
            ],
        )
        values = facts.to_fact_values()
        assert values["construction_in_progress"] == pytest.approx(9.5328e9)
        assert values["fixed_assets"] == pytest.approx(1.8928e10)
        assert values["net_profit_dedt"] == pytest.approx(1.6477e10)


# ═══════════════════════════════════════════════════════════════════════════
# 4. 渲染（FactCollector 的工具输出）
# ═══════════════════════════════════════════════════════════════════════════


class TestRender:
    def test_capacity_section_rendered(self):
        text = render(_raw_data())
        assert "产能与扣非" in text
        assert "在建工程" in text
        # 9,532,794,486.59 元 → 95.33 亿元
        assert "| 95.33 |" in text

    def test_missing_values_render_as_dash_not_zero(self):
        data = _raw_data()
        for row in data["balance"]:
            row["construction_in_progress"] = None
            row["fixed_assets"] = None
        for row in data["fina"]:
            row["net_profit_dedt"] = None
            row["depreciation_amortization"] = None
        text = render(data)
        section = text.split("### 产能与扣非")[1].split("###")[0]
        # 四列全缺 ⇒ 不打印数据行，更不能把"未披露"渲染成 0.00
        assert "0.00" not in section
