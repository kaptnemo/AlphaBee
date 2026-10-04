"""TTM 还原与 net_profit_ttm_yoy 口径测试。

背景：Tushare income 返回报告期累计值（三季报 = 前三季度累计），
而 PE-TTM 是滚动 12 个月口径；PEG 的分子分母必须同口径。

契约：
- 累计值 → TTM：TTM_t = 上年年报累计 + 本期累计 − 上年同期累计；
- 数据不足 / 去年同期 TTM ≤ 0（基期亏损，增速无意义）→ 显式 None，不猜、不补零；
- FinancialFacts.net_profit_ttm_yoy 与 PE-TTM 同为滚动 12 个月口径；
- to_fact_values() 导出该字段供 DerivedFacts 引擎使用。
"""

from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot
from alphabee.agents.facts.ttm import ttm_from_cumulative, ttm_yoy


def _snapshots(series: dict[str, float | None]) -> list[FinancialSnapshot]:
    """按报告期倒序构造快照（series: 报告期 → 归母净利润累计值）。"""
    return [
        FinancialSnapshot(period=period, net_profit_attr_p=value)
        for period, value in sorted(series.items(), reverse=True)
    ]


# ── 纯函数：累计值 → TTM ───────────────────────────────────────────────────


def test_ttm_from_cumulative_quarter_case():
    # 2024Q3 TTM = FY2023 + 9M2024 − 9M2023 = 80 + 70 − 60
    series = {"20241231": 100.0, "20240930": 70.0, "20231231": 80.0, "20230930": 60.0}
    assert ttm_from_cumulative(series, "20240930") == 90.0


def test_ttm_from_cumulative_annual_case_degenerates_to_fy():
    # 年报：TTM 就是当年年报本身
    series = {"20241231": 100.0, "20231231": 80.0, "20221231": 50.0}
    assert ttm_from_cumulative(series, "20241231") == 100.0


def test_ttm_from_cumulative_returns_none_when_history_incomplete():
    # 缺上年年报（新股/历史窗口不足）
    assert ttm_from_cumulative({"20240930": 70.0, "20230930": 60.0}, "20240930") is None
    # 缺上年同期（去年同期未披露）
    assert ttm_from_cumulative({"20240930": 70.0, "20231231": 80.0}, "20240930") is None
    # 非法报告期
    assert ttm_from_cumulative({"20240930": 70.0}, "2024Q3") is None


def test_ttm_yoy_matches_rolling_window():
    # TTM_2024Q3 = 80 + 70 − 60 = 90；TTM_2023Q3 = 50 + 60 − 40 = 70
    series = {
        "20240930": 70.0,
        "20231231": 80.0,
        "20230930": 60.0,
        "20221231": 50.0,
        "20220930": 40.0,
    }
    assert ttm_yoy(series, "20240930") == (90.0 - 70.0) / 70.0 * 100.0


def test_ttm_yoy_differs_from_cumulative_yoy():
    # 累计同比 = 70/60−1 = +16.7%，TTM 同比 = 90/70−1 = +28.6%：
    # 这正是分子分母口径错配会产生偏差的场景（Q4 基数偏弱）
    series = {
        "20240930": 70.0,
        "20231231": 80.0,
        "20230930": 60.0,
        "20221231": 50.0,
        "20220930": 40.0,
    }
    cumulative_yoy = (70.0 - 60.0) / 60.0 * 100.0
    assert ttm_yoy(series, "20240930") > cumulative_yoy


def test_ttm_yoy_none_when_base_period_is_loss():
    # 去年同期 TTM 为负（亏损）→ 增速无经济含义
    series = {"20240930": 20.0, "20231231": -10.0, "20230930": -5.0, "20221231": -8.0, "20220930": -6.0}
    assert ttm_yoy(series, "20240930") is None


# ── FinancialFacts 集成 ────────────────────────────────────────────────────


def test_financial_facts_net_profit_ttm_yoy_and_export():
    facts = FinancialFacts(
        stock_code="600519.SH",
        snapshots=_snapshots(
            {
                "20240930": 70.0,
                "20231231": 80.0,
                "20230930": 60.0,
                "20221231": 50.0,
                "20220930": 40.0,
            }
        ),
    )
    assert facts.net_profit_ttm_yoy == (90.0 - 70.0) / 70.0 * 100.0
    assert facts.to_fact_values()["net_profit_ttm_yoy"] == facts.net_profit_ttm_yoy


def test_financial_facts_net_profit_ttm_yoy_absent_when_history_short():
    facts = FinancialFacts(stock_code="301234.SZ", snapshots=_snapshots({"20240930": 70.0, "20240630": 50.0}))
    assert facts.net_profit_ttm_yoy is None
    # 不可用时字段不出现在 fact_values 中（而不是以 0/None 混入）
    assert "net_profit_ttm_yoy" not in facts.to_fact_values()


def test_financial_facts_ttm_uses_attributable_profit_not_total():
    # 归母口径：net_profit（含少数股东）不同不应影响 TTM 同比
    facts = FinancialFacts(
        stock_code="000001.SZ",
        snapshots=[
            FinancialSnapshot(period="20240930", net_profit=999.0, net_profit_attr_p=70.0),
            FinancialSnapshot(period="20231231", net_profit=888.0, net_profit_attr_p=80.0),
            FinancialSnapshot(period="20230930", net_profit=777.0, net_profit_attr_p=60.0),
            FinancialSnapshot(period="20221231", net_profit=666.0, net_profit_attr_p=50.0),
            FinancialSnapshot(period="20220930", net_profit=555.0, net_profit_attr_p=40.0),
        ],
    )
    assert facts.net_profit_ttm_yoy == (90.0 - 70.0) / 70.0 * 100.0
