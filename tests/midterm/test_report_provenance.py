"""P2-1：报告期 provenance（report_ann_date / report_age_days）与报告「陈旧」标记测试。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §13：

- ``report_ann_date`` 取 L 所用最新记录（fina 最新；缺失回落 income 最新）的 ann_date，
  统一为 ``YYYY-MM-DD``；
- ``report_age_days`` 只由 ``get_factor_snapshot`` 在构建后按 as_of_date=today 计算，
  builder（纯函数）不读系统时间；
- reporter 在 ``report_period`` 非空时追加「F 数据期」行；``>120`` 天标「**陈旧**」；
  旧帧（无字段/无 factor）不新增行；
- 纯展示/provenance，F 值不变。
"""

from __future__ import annotations

from typing import Any

import pytest

from alphabee.midterm.factors import _report_age_days, build_fundamental_factor
from alphabee.midterm.models import (
    CompanyStateArtifact,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
)
from alphabee.midterm.score_engine import fundamental_breakdown
from alphabee.orchestrator.nodes import midterm_reporter as reporter

_BREAKDOWN = {
    "revenue_yoy": 10.0,
    "revenue_term": 0.3333,
    "revenue_saturated": 0.0,
    "growth_mean": 0.3333,
    "f": 0.3333,
    "raw": 0.3333,
    "output_saturated": 0.0,
}


def _fin_data(*, fina_ann: str | None = None, income_ann: str | None = None) -> dict[str, Any]:
    fina: dict[str, Any] = {"period": "20250930", "revenue_yoy": 10.0}
    if fina_ann is not None:
        fina["ann_date"] = fina_ann
    income: dict[str, Any] = {"period": "20250930", "revenue": 1.0}
    if income_ann is not None:
        income["ann_date"] = income_ann
    return {"fina": [fina], "income": [income], "cashflow": [], "balance": []}


def _decision(
    *,
    report_period: str = "20260930",
    ann_date: str = "2026-10-31",
    age: int | None = 2,
) -> CompanyStateArtifact:
    snapshot = FactorSnapshot(
        symbol="600760.SH",
        as_of_date="2026-11-02",
        fundamental=FundamentalFactor(
            revenue_yoy=10.0,
            report_period=report_period,
            report_ann_date=ann_date,
            report_age_days=age,
            score_breakdown=dict(_BREAKDOWN),
        ),
    )
    return CompanyStateArtifact(symbol="600760.SH", as_of_date="2026-11-02", factor_snapshot=snapshot)


# ─────────────────────────────────────────────────────────────────────────────
# builder：report_ann_date 来源与格式
# ─────────────────────────────────────────────────────────────────────────────


def test_builder_sets_report_period_and_ann_date():
    """builder 写入 report_period + report_ann_date（统一 YYYY-MM-DD）。"""
    factor, _ = build_fundamental_factor(_fin_data(fina_ann="20251028"))

    assert factor.report_period == "20250930"
    assert factor.report_ann_date == "2025-10-28"
    assert factor.report_age_days is None  # builder 纯函数不读系统时间（只由编排层计算）


def test_builder_ann_date_falls_back_to_income_and_handles_missing():
    """fina ann_date 缺失/非法 ⇒ 回落 income 最新；两处都缺 ⇒ ""（不猜测）。"""
    assert build_fundamental_factor(_fin_data(income_ann="2025-10-20"))[0].report_ann_date == "2025-10-20"
    assert build_fundamental_factor(_fin_data(fina_ann="nan", income_ann="20251020"))[0].report_ann_date == "2025-10-20"
    assert build_fundamental_factor(_fin_data())[0].report_ann_date == ""


def test_builder_ann_date_accepts_dashed_and_compact():
    """源格式兼容：2026-10-31 / 20261031 都归一为 2026-10-31。"""
    assert build_fundamental_factor(_fin_data(fina_ann="2026-10-31"))[0].report_ann_date == "2026-10-31"
    assert build_fundamental_factor(_fin_data(fina_ann="20261031"))[0].report_ann_date == "2026-10-31"


# ─────────────────────────────────────────────────────────────────────────────
# _report_age_days：缺失/非法/当天/128 天边界
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ann_date", "as_of", "expected"),
    [
        ("2026-10-30", "2026-10-30", 0),  # 当天 ⇒ 0（不是 None）
        ("2025-11-14", "2026-03-22", 128),  # 688981 A+H 式陈旧样本
        ("20250624", "2025-10-30", 128),  # 紧凑格式同样可算
        ("", "2026-10-30", None),  # 公告日缺失
        ("nan", "2026-10-30", None),  # 非法
        ("2025-13-40", "2026-10-30", None),  # 非法日历日期（月/日越界）
        ("2026-10-30", "", None),  # as_of 缺失
        ("2026-10-30", "not-a-date", None),  # as_of 非法
    ],
)
def test_report_age_days_boundaries(ann_date: str, as_of: str, expected: int | None):
    assert _report_age_days(ann_date, as_of) == expected


def test_report_provenance_does_not_change_f():
    """纯展示/provenance：新增字段不进入 F 计算，分解逐值不变。"""
    base = FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=20.0, eps_growth_yoy=15.0)
    with_provenance = base.model_copy(
        update={
            "report_period": "20260930",
            "report_ann_date": "2026-10-31",
            "report_age_days": 2,
        }
    )
    assert fundamental_breakdown(with_provenance, ExpectationFactor()) == fundamental_breakdown(
        base, ExpectationFactor()
    )


# ─────────────────────────────────────────────────────────────────────────────
# reporter：F 数据期行（含/不含陈旧两态、公告未知、旧帧不加行）
# ─────────────────────────────────────────────────────────────────────────────


def test_reporter_renders_fresh_data_period_line():
    text = reporter.render_midterm_decision_summary(_decision())
    assert "F 数据期: 2026Q3（公告 2026-10-31，距今 2 天）" in text
    assert "**陈旧**" not in text


def test_reporter_marks_stale_after_120_days():
    """688981 A+H 式：2025Q3 公告 2025-11-14，128 天后 ⇒ 追加「**陈旧**」。"""
    text = reporter.render_midterm_decision_summary(_decision(report_period="20250930", ann_date="2025-11-14", age=128))
    assert "F 数据期: 2025Q3（公告 2025-11-14，距今 128 天，**陈旧**）" in text


def test_reporter_stale_threshold_is_strictly_greater_than_120():
    """120 天恰等阈值 ⇒ 不算陈旧（>120 才标）；121 天 ⇒ 陈旧。"""
    fresh = reporter.render_midterm_decision_summary(_decision(age=120))
    stale = reporter.render_midterm_decision_summary(_decision(age=121))
    assert "F 数据期: 2026Q3（公告 2026-10-31，距今 120 天）" in fresh
    assert "**陈旧**" not in fresh
    assert "F 数据期: 2026Q3（公告 2026-10-31，距今 121 天，**陈旧**）" in stale


def test_reporter_ann_date_missing_shows_explicit_unknown():
    """公告日缺失 ⇒ 显式「公告未知」（不静默隐藏，也不标陈旧）。"""
    text = reporter.render_midterm_decision_summary(_decision(ann_date="", age=None))
    assert "F 数据期: 2026Q3（公告未知）" in text
    assert "**陈旧**" not in text


def test_reporter_old_frame_without_report_period_adds_no_line():
    """旧帧（无 report_period/无 factor）不新增行，既有报告行保持原样。"""
    old_factor_text = reporter.render_midterm_decision_summary(_decision(report_period="", ann_date="", age=None))
    assert "F 数据期" not in old_factor_text

    no_factor = CompanyStateArtifact(symbol="600760.SH", as_of_date="2026-11-02")
    text = reporter.render_midterm_decision_summary(no_factor)
    assert "F 数据期" not in text
    assert "F 分解" not in text  # 既有「旧帧不渲染分解」语义不变


def test_reporter_quarters_for_each_period_suffix():
    """数据期标签按报告期后缀映射 Q1–Q4。"""
    for period, label in (
        ("20260331", "2026Q1"),
        ("20260630", "2026Q2"),
        ("20260930", "2026Q3"),
        ("20261231", "2026Q4"),
    ):
        text = reporter.render_midterm_decision_summary(_decision(report_period=period, ann_date="2026-10-31", age=2))
        assert f"F 数据期: {label}（公告 2026-10-31，距今 2 天）" in text
