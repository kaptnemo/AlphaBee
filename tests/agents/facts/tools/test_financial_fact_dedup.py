"""财务事实去重（P0-2）单元测试：合并报表优先 + 最新公告/修订 + 非空优先。

Tushare 同一报告期可能返回多行（修订 ``update_flag``、更正公告 ``ann_date``、
合并/母公司报表 ``report_type``）；旧实现 ``drop_duplicates(keep="first")``
依赖返回行序，会取到初值/快报值。本文件全部用合成 DataFrame，不发起网络请求；
真实接口（688981/000002）的去重实测见 ``test_integration.py`` 的 integration 用例。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

import alphabee.agents.facts.tools.financial_fact as financial_fact_mod
from alphabee.adapters.tushare import TuShare_Adapter
from alphabee.agents.facts.tools.financial_fact import (
    _FINA_NONNULL_PREFERENCE,
    _dedup,
    get_financial_fact,
)


@pytest.fixture(autouse=True)
def _clear_financial_cache():
    """清空 financial_fact 的 TTL 缓存，保证 fake helper 测试互不污染。"""

    def _clear() -> None:
        financial_fact_mod._CACHE._cache.clear()
        financial_fact_mod._CACHE._inflight.clear()

    _clear()
    yield
    _clear()


# ─────────────────────────────────────────────────────────────────────────────
# 688981 实况：同 period 三行 → 最新公告 + 最新修订
# ─────────────────────────────────────────────────────────────────────────────


def test_688981_2024_annual_takes_latest_revision():
    """688981 2024 年报三行：唯一首行是 2025-03-28 的最终修订 57,795,570,000。

    旧口径（keep="first"）会保留返回行序首条 57,745,538,740.4（2025-02-12 初值）。
    """
    df = pd.DataFrame(
        [
            {
                "period": "20241231",
                "ann_date": "20250212",
                "report_type": "1",
                "update_flag": 0,
                "revenue": 57_745_538_740.4,
            },
            {
                "period": "20241231",
                "ann_date": "20250212",
                "report_type": "1",
                "update_flag": 1,
                "revenue": 57_795_570_000.0,
            },
            {
                "period": "20241231",
                "ann_date": "20250328",
                "report_type": "1",
                "update_flag": 1,
                "revenue": 57_795_570_000.0,
            },
        ]
    )

    out = _dedup(df)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["revenue"] == pytest.approx(57_795_570_000.0)
    assert row["ann_date"] == "20250328"  # provenance 随行保留
    # Tushare 内部列不得泄漏到下游
    assert "report_type" not in out.columns
    assert "update_flag" not in out.columns


def test_update_flag_decides_when_ann_date_ties():
    """同报告期同公告日（修订）：``update_flag=1`` 的修订行优先。"""
    df = pd.DataFrame(
        [
            {"period": "20241231", "ann_date": "20250212", "update_flag": 0, "revenue": 100.0},
            {"period": "20241231", "ann_date": "20250212", "update_flag": 1, "revenue": 200.0},
        ]
    )
    out = _dedup(df)
    assert len(out) == 1
    assert out.iloc[0]["revenue"] == pytest.approx(200.0)


def test_multiple_periods_are_sorted_descending():
    """不同报告期各自去重，整体仍按 period 降序（ref_dates 依赖该顺序）。"""
    df = pd.DataFrame(
        [
            {"period": "20240630", "ann_date": "20240830", "revenue": 2.0},
            {"period": "20241231", "ann_date": "20250212", "revenue": 9.0},
            {"period": "20241231", "ann_date": "20250328", "update_flag": 1, "revenue": 10.0},
            {"period": "20240930", "ann_date": "20241101", "revenue": 5.0},
        ]
    )
    out = _dedup(df)
    assert out["period"].tolist() == ["20241231", "20240930", "20240630"]
    assert out.iloc[0]["revenue"] == pytest.approx(10.0)


# ─────────────────────────────────────────────────────────────────────────────
# fina_indicator：000002 实况 + 非空字段优先
# ─────────────────────────────────────────────────────────────────────────────


def test_000002_fina_nan_row_does_not_win():
    """000002 2026Q3 fina 两行：``update_flag=1`` 行有扣非值，``0`` 行为 NaN。"""
    df = pd.DataFrame(
        [
            {
                "period": "20260930",
                "ann_date": "20261031",
                "update_flag": 0,
                "net_profit_dedt": math.nan,
                "gross_margin": math.nan,
            },
            {
                "period": "20260930",
                "ann_date": "20261031",
                "update_flag": 1,
                "net_profit_dedt": -2.153752e10,
                "gross_margin": 8.0,
            },
        ]
    )
    out = _dedup(df, prefer_nonnull=_FINA_NONNULL_PREFERENCE)
    assert len(out) == 1
    row = out.iloc[0]
    assert not pd.isna(row["net_profit_dedt"])
    assert row["net_profit_dedt"] == pytest.approx(-2.153752e10)
    assert not pd.isna(row["gross_margin"])


def test_fina_tie_breaks_on_nonnull_count():
    """period/ann_date/update_flag 完全并列时：非空字段计数降序优先（稳定排序）。"""
    df = pd.DataFrame(
        [
            {
                "period": "20260930",
                "ann_date": "20261031",
                "update_flag": 1,
                "net_profit_dedt": math.nan,
                "gross_margin": math.nan,
                "roe": 1.0,
            },
            {
                "period": "20260930",
                "ann_date": "20261031",
                "update_flag": 1,
                "net_profit_dedt": -2.0e10,
                "gross_margin": 9.0,
                "roe": 2.0,
            },
        ]
    )
    out = _dedup(df, prefer_nonnull=_FINA_NONNULL_PREFERENCE)
    assert len(out) == 1
    assert out.iloc[0]["net_profit_dedt"] == pytest.approx(-2.0e10)
    # 对照：不传 prefer_nonnull 时按稳定排序取原顺序首行（本用例专门钉住差异）
    baseline = _dedup(df)
    assert pd.isna(baseline.iloc[0]["net_profit_dedt"])


# ─────────────────────────────────────────────────────────────────────────────
# report_type：合并报表优先 / 无 1 时 fail-open
# ─────────────────────────────────────────────────────────────────────────────


def test_report_type_prefers_consolidated_statement():
    """同 period 同时有合并（1）与母公司（4）报表 ⇒ 只保留合并报表。

    夹具刻意让**母公司行在前且公告日更晚**（20250401 > 20250328）：若实现漏掉
    ``report_type`` 过滤、只按公告日排序，就会取到母公司行的 999.0 而转红——这样
    用例钉的是「合并报表优先于更新时间」，而不是碰巧被更晚公告日选中。
    """
    for report_type in ("1", 1, 1.0):  # 字符串/整数/浮点三种返回形态都要归一
        df = pd.DataFrame(
            [
                # 母公司报表：排在首行，且公告日更晚
                {"period": "20241231", "ann_date": "20250401", "report_type": 4, "revenue": 999.0},
                # 合并报表：排在后面，公告日更早
                {"period": "20241231", "ann_date": "20250328", "report_type": report_type, "revenue": 100.0},
            ]
        )
        out = _dedup(df)
        assert len(out) == 1
        assert out.iloc[0]["revenue"] == pytest.approx(100.0)  # 合并报表优先于更新时间
        assert out.iloc[0]["ann_date"] == "20250328"  # 保留的是合并行而非更晚公告的母公司行


def test_report_type_without_1_fails_open():
    """无 ``report_type=1``（例如只有母公司/单季报表）⇒ 保留全部再按公告日去重。"""
    df = pd.DataFrame(
        [
            {"period": "20241231", "ann_date": "20250212", "report_type": "4", "revenue": 100.0},
            {"period": "20241231", "ann_date": "20250328", "report_type": "5", "revenue": 200.0},
        ]
    )
    out = _dedup(df)
    assert len(out) == 1
    assert out.iloc[0]["revenue"] == pytest.approx(200.0)  # 最新公告日胜出（未因过滤丢数据）


# ─────────────────────────────────────────────────────────────────────────────
# ann_date 缺失/非法：回落 period 排序
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", [None, math.nan, "nan", "", "None"])
def test_ann_date_missing_falls_back_to_period(missing):
    """ann_date 缺失/非法 ⇒ 回落 period（"nan" 字符串不得排到数字之前）。"""
    df = pd.DataFrame(
        [
            {"period": "20241231", "ann_date": missing, "revenue": 100.0},
            {"period": "20241231", "ann_date": "20250328", "revenue": 200.0},
        ]
    )
    out = _dedup(df)
    assert len(out) == 1
    assert out.iloc[0]["revenue"] == pytest.approx(200.0)  # 合法公告日晚于 period 后缀


def test_ann_date_dashed_format_normalized():
    """``2025-03-28`` 与 ``20250212`` 同口径比较；带分隔符的更晚公告日胜出。"""
    df = pd.DataFrame(
        [
            {"period": "20241231", "ann_date": "2025-02-12", "revenue": 100.0},
            {"period": "20241231", "ann_date": "2025-03-28", "revenue": 200.0},
        ]
    )
    out = _dedup(df)
    assert len(out) == 1
    assert out.iloc[0]["revenue"] == pytest.approx(200.0)


# ─────────────────────────────────────────────────────────────────────────────
# 列治理：内部列清理，ann_date / company_type 保留；空输入原样返回
# ─────────────────────────────────────────────────────────────────────────────


def test_internal_columns_dropped_but_ann_date_and_company_type_kept():
    df = pd.DataFrame(
        [
            {
                "period": "20260930",
                "ann_date": "20261030",
                "report_type": "1",
                "update_flag": 1,
                "company_type": "3",
                "revenue": 1.0e9,
            }
        ]
    )
    out = _dedup(df)
    assert set(out.columns) == {"period", "ann_date", "company_type", "revenue"}
    assert out.iloc[0]["ann_date"] == "20261030"
    assert out.iloc[0]["company_type"] == "3"


def test_empty_and_none_inputs_returned_as_is():
    assert _dedup(None) is None
    empty = pd.DataFrame(columns=["period", "ann_date", "revenue"])
    assert _dedup(empty).empty
    assert list(_dedup(empty).columns) == ["period", "ann_date", "revenue"]


# ─────────────────────────────────────────────────────────────────────────────
# 适配器映射：income 两个营收口径必须落到不同列，且无重复列名
# ─────────────────────────────────────────────────────────────────────────────


def test_income_adapter_maps_two_revenue_fields_to_distinct_columns():
    """Tushare income 同时返回 total_revenue/revenue：映射为 revenue/operating_revenue。

    旧映射只有 total_revenue→revenue；新增 revenue→operating_revenue 后必须
    保持两列、且 canonical 列名不重复（重复列名会让下游按名取列取到错误口径）。
    """
    raw = pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "ann_date": "20261030",
                "end_date": "20260930",
                "report_type": "1",
                "update_flag": "1",
                "comp_type": "1",
                "total_revenue": 1309.04e8,
                "revenue": 1284.54e8,
            }
        ]
    )

    adapted = TuShare_Adapter.adapt("income", raw)

    assert len(adapted.columns) == len(set(adapted.columns))  # 无重复 canonical 列名
    row = adapted.iloc[0]
    assert row["stock_code"] == "600519.SH"
    assert row["period"] == "20260930"
    assert row["revenue"] == pytest.approx(1309.04e8)  # total_revenue → revenue（语义不变）
    assert row["operating_revenue"] == pytest.approx(1284.54e8)  # revenue → operating_revenue
    assert row["company_type"] == "1"
    assert "total_revenue" not in adapted.columns
    assert "comp_type" not in adapted.columns
    # provenance / 内部列：ann_date 原样保留，report_type/update_flag 也原样保留（由 _dedup 清理）
    assert row["ann_date"] == "20261030"


def test_fina_indicator_adapter_maps_dt_netprofit_yoy():
    raw = pd.DataFrame(
        [{"ts_code": "000002.SZ", "end_date": "20260930", "update_flag": "1", "dt_netprofit_yoy": 18.68}]
    )
    adapted = TuShare_Adapter.adapt("fina_indicator", raw)
    assert adapted.iloc[0]["net_profit_dedt_yoy"] == pytest.approx(18.68)
    assert "dt_netprofit_yoy" not in adapted.columns


# ─────────────────────────────────────────────────────────────────────────────
# 全链路（fake TushareHelper）：ref_dates 降序 + 四表去重/provenance
# ─────────────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, data: pd.DataFrame) -> None:
        self.data = data


class _FakeHelper:
    """模拟 TuShareHelper：返回已适配（canonical 列名）的确定性 DataFrame。"""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def __enter__(self) -> _FakeHelper:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def income(self, **kwargs: object) -> _FakeResult:
        return _FakeResult(self._frames["income"])

    def balancesheet(self, **kwargs: object) -> _FakeResult:
        return _FakeResult(self._frames["balance"])

    def cashflow(self, **kwargs: object) -> _FakeResult:
        return _FakeResult(self._frames["cashflow"])

    def fina_indicator(self, **kwargs: object) -> _FakeResult:
        return _FakeResult(self._frames["fina"])


def test_get_financial_fact_ref_dates_descending_and_each_table_deduped(monkeypatch):
    frames = {
        # 故意乱序 + 同 period 两行（修正行更晚）
        "income": pd.DataFrame(
            [
                {"period": "20240630", "ann_date": "20240830", "revenue": 2.0},
                {"period": "20241231", "ann_date": "20250212", "update_flag": 0, "revenue": 9.0},
                {"period": "20241231", "ann_date": "20250328", "update_flag": 1, "revenue": 10.0},
                {"period": "20240930", "ann_date": "20241101", "revenue": 5.0},
            ]
        ),
        "balance": pd.DataFrame(
            [
                {"period": "20240630", "ann_date": "20240830", "shareholders_equity": 1.0},
                {"period": "20241231", "ann_date": "20250212", "update_flag": 0, "shareholders_equity": 2.0},
                {"period": "20241231", "ann_date": "20250328", "update_flag": 1, "shareholders_equity": 3.0},
            ]
        ),
        "cashflow": pd.DataFrame(
            [
                {"period": "20241231", "ann_date": "20250212", "update_flag": 0, "operating_cashflow": 1.0},
                {"period": "20241231", "ann_date": "20250328", "update_flag": 1, "operating_cashflow": 2.0},
            ]
        ),
        "fina": pd.DataFrame(
            [
                {"period": "20241231", "ann_date": "20250212", "update_flag": 0, "net_profit_dedt": math.nan},
                {"period": "20241231", "ann_date": "20250212", "update_flag": 1, "net_profit_dedt": 7.0},
            ]
        ),
    }
    monkeypatch.setattr(financial_fact_mod, "TuShareHelper", lambda: _FakeHelper(frames))

    data = get_financial_fact("688981.SH", periods=3)

    assert data["ref_dates"] == ["20241231", "20240930", "20240630"]  # 仍为降序
    assert {row["period"] for row in data["income"]} == {"20241231", "20240930", "20240630"}
    assert data["income"][0]["revenue"] == pytest.approx(10.0)  # 最新修订
    assert data["income"][0]["ann_date"] == "20250328"
    assert data["balance"][0]["shareholders_equity"] == pytest.approx(3.0)
    assert data["cashflow"][0]["operating_cashflow"] == pytest.approx(2.0)
    assert data["fina"][0]["net_profit_dedt"] == pytest.approx(7.0)
    for table in ("income", "balance", "cashflow", "fina"):
        assert "update_flag" not in data[table][0]
        assert "report_type" not in data[table][0]
        assert "ann_date" in data[table][0]
