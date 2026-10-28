"""consensus 聚合引擎单测（纯函数，无网络）。

覆盖四组契约：

1. **财年对齐**：东财三个 EPS 字段是「相对发布年」的，必须按
   ``slot = 目标财年 - publishDate.year`` 解析；否则跨年会把 FY2026/FY2027 混进同一中位数，
   并在 1 月伪造出「增长率级」的 revision。
2. **快照口径**：每家机构取各自最新一篇 → 横截面中位数；机构数不足
   ``MIN_SNAPSHOT_ORGS`` → 值置 None + ``consensus_missing``（宁缺勿错）。
3. **revision / 加速度**：同一锚定财年的 as-of 回放；加速度是固定 30 天网格上的
   ``r(t) - r(t-30)``，且 ``r(t)`` 与对外披露的 ``eps_fy1_revision_1m`` 同源。
4. DATA_CONTRACTS_VERIFIED.md §1 的既有契约：emRatingValue 方向（持有1/增持2/买入3）、
   评级方向差分禁止读 ratingChange、目标价低覆盖降级。
"""

from __future__ import annotations

import pytest

from alphabee.collectors.consensus.engine import (
    CONSENSUS_FIELDS,
    aggregate_consensus,
)

ASOF = "2026-10-26"
ORGS = ("机构A", "机构B", "机构C")


def _report(
    publish_date: str,
    *,
    org: str = "机构A",
    org_code: str | None = None,
    eps1=None,
    eps2=None,
    eps3=None,
    target=None,
    rating_value=None,
    last_rating_value=None,
    rating_change=None,
):
    return {
        "publishDate": publish_date,
        "orgCode": org if org_code is None else org_code,
        "orgSName": org,
        "predictThisYearEps": eps1,
        "predictNextYearEps": eps2,
        "predictNextTwoYearEps": eps3,
        "indvAimPriceT": target,
        "emRatingValue": rating_value,
        "lastEmRatingValue": last_rating_value,
        "ratingChange": rating_change,
    }


def _batch(spec, *, field: str = "eps1", orgs: tuple[str, ...] = ORGS):
    """``[(publish_date, value), ...]`` → 每个机构在同一批日期发同样的预测。"""
    return [_report(d, org=o, **{field: v}) for d, v in spec for o in orgs]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 财年对齐
# ─────────────────────────────────────────────────────────────────────────────
def test_fiscal_year_alignment_across_publish_years():
    # 锚定财年 = as_of 所在自然年（2027）。老研报通过 slot=1 贡献 FY2027，不应把
    # FY2026 的 1.00 混进中位数。
    reports = [
        _report("2026-12-20 00:00:00.000", org="机构A", eps1=1.00, eps2=1.30),
        _report("2027-01-03 00:00:00.000", org="机构B", eps1=1.28),
        _report("2026-11-10 00:00:00.000", org="机构C", eps1=0.98, eps2=1.26),
    ]
    out = aggregate_consensus(reports, as_of_date="2027-01-05")

    assert out.fy_anchor == 2027
    assert out.values["eps_fy1"] == pytest.approx(1.28)  # median(1.30, 1.28, 1.26)
    assert out.values["eps_fy1"] != pytest.approx(1.00)  # 未对齐口径会给 median(1.00, 1.28, 0.98)


def test_revision_uses_same_fiscal_year_across_rollover():
    # 12 月研报（FY2027 走 slot=1）与 1 月研报（FY2027 走 slot=0）对同一财年给同样的数
    # → revision 应≈0（真实修正），而不是 +30%（FY2027/FY2026 的增长率）。
    reports = [
        *[_report("2026-11-20 00:00:00.000", org=o, eps2=1.30) for o in ORGS],
        *[_report("2027-01-03 00:00:00.000", org=o, eps1=1.30) for o in ORGS],
    ]
    out = aggregate_consensus(reports, as_of_date="2027-01-05")

    assert out.values["eps_fy1"] == pytest.approx(1.30)
    assert out.values["eps_fy1_revision_1m"] == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 快照口径与样本量护栏
# ─────────────────────────────────────────────────────────────────────────────
def test_eps_median_is_latest_per_org_cross_section():
    # 每家机构取各自最新一篇，再取横截面中位数（不是"最新一天发布者的中位数"）
    reports = [
        _report("2026-09-01 00:00:00.000", org="机构A", eps1=2.00),
        _report("2026-10-20 00:00:00.000", org="机构B", eps1=2.40),
        _report("2026-10-22 00:00:00.000", org="机构C", eps1=2.20),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1"] == pytest.approx(2.20)
    assert out.values["eps_fy1"] != pytest.approx(2.40)  # 不是"只剩最新一天那家"
    assert out.eps_basis["eps_fy1"] == 3


def test_eps_median_requires_min_orgs():
    reports = [
        _report("2026-10-20 00:00:00.000", org="机构A", eps1=2.00),
        _report("2026-10-22 00:00:00.000", org="机构B", eps1=2.40),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1"] is None
    assert "consensus_missing: eps_fy1" in out.missing
    assert out.eps_basis["eps_fy1"] == 2


def test_coverage_count_falls_back_to_org_name():
    # orgCode 缺失（换源/裁剪字段）时不能静默退化成研报条数
    reports = [
        {"publishDate": "2026-10-01 00:00:00.000", "orgSName": "机构A"},
        {"publishDate": "2026-10-02 00:00:00.000", "orgSName": "机构A"},
        {"publishDate": "2026-10-03 00:00:00.000", "orgSName": "机构B"},
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["coverage_count"] == 2


def test_empty_reports_all_missing():
    out = aggregate_consensus([], as_of_date=ASOF)

    assert out.values["coverage_count"] is None
    for f in CONSENSUS_FIELDS:
        assert out.values[f] is None
    assert all(f"consensus_missing: {f}" in out.missing for f in CONSENSUS_FIELDS)
    assert out.fy_anchor == 2026
    assert out.report_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. revision 与加速度
# ─────────────────────────────────────────────────────────────────────────────
def test_revision_1m_basic():
    reports = _batch([("2026-09-16", 1.00), ("2026-10-16", 1.10)])
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1_revision_1m"] == pytest.approx(10.0)  # (1.10/1.00 - 1) * 100
    assert out.eps_basis["eps_fy1_revision_1m"] == 3


def test_revision_non_positive_base_is_missing():
    # 基线为亏损（FY2026 < 0）→ 百分比修正无意义，必须置 None，而不是 -480%
    reports = _batch([("2026-08-10", -0.50), ("2026-10-10", 1.90)])
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1"] == pytest.approx(1.90)
    assert out.values["eps_fy1_revision_1m"] is None
    assert "consensus_missing: eps_fy1_revision_1m" in out.missing


def test_revision_acceleration_uses_fixed_30d_grid():
    # t / t-30 / t-60 三个快照：E = 1.15 / 1.05 / 1.00
    # r(t) = 9.5238%，r(t-30) = 5.0% → 加速度 = +4.5238pp
    reports = _batch([("2026-08-17", 1.00), ("2026-09-16", 1.05), ("2026-10-16", 1.15)])
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1"] == pytest.approx(1.15)
    assert out.values["eps_fy1_revision_1m"] == pytest.approx(9.5238)
    assert out.values["revision_acceleration"] == pytest.approx(4.5238)
    assert out.eps_basis["revision_acceleration"] == 3


def test_revision_acceleration_deceleration():
    # E = 1.12 / 1.10 / 1.00 → r(t)=1.8182%，r(t-30)=10.0% → 减速 -8.1818pp
    reports = _batch([("2026-08-17", 1.00), ("2026-09-16", 1.10), ("2026-10-16", 1.12)])
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["revision_acceleration"] == pytest.approx(-8.1818)


def test_revision_acceleration_missing_without_second_point():
    # 缺 t-60 快照 → 只有一个可算 revision 时点 → 加速度 None + missing
    reports = _batch([("2026-09-16", 1.05), ("2026-10-16", 1.15)])
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1_revision_1m"] == pytest.approx(9.5238)
    assert out.values["revision_acceleration"] is None
    assert "consensus_missing: revision_acceleration" in out.missing


# ─────────────────────────────────────────────────────────────────────────────
# 4. 既有契约：评级 / 目标价 / 字符串归一
# ─────────────────────────────────────────────────────────────────────────────
def test_rating_mean_uses_emratingvalue_bullish_direction():
    # 买入=3 / 增持=2 / 持有=1 → 均值越大越看多
    reports = [
        _report("2026-09-06 00:00:00.000", rating_value="3"),
        _report("2026-09-06 00:00:00.000", rating_value="2"),
        _report("2026-09-06 00:00:00.000", rating_value="3"),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert abs(out.values["rating_mean"] - (8 / 3)) < 1e-9


def test_rating_direction_uses_emratingvalue_diff_not_ratingchange():
    reports = [
        # 上调：3 vs 2
        _report("2026-10-01 00:00:00.000", rating_value=3, last_rating_value=2),
        # 下调：1 vs 2
        _report("2026-10-02 00:00:00.000", rating_value=1, last_rating_value=2),
        # ratingChange=1 但 emRatingValue==lastEmRatingValue → 不算上调（ratingChange 被忽略）
        _report(
            "2026-10-03 00:00:00.000",
            rating_value=2,
            last_rating_value=2,
            rating_change=1,
        ),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["rating_upgrade_1m"] == 1
    assert out.values["rating_downgrade_1m"] == 1
    assert out.values["revision_breadth"] == 0.5


def test_target_price_low_coverage_degrades_to_missing():
    reports = [_report("2026-09-06 00:00:00.000", target=100.0)] + [
        _report("2026-09-06 00:00:00.000", target=None, org=f"机构{i}") for i in range(9)
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF, target_price_coverage_threshold=0.3)

    assert out.values["target_price"] is None  # 覆盖率 1/10 < 0.3
    assert "consensus_missing: target_price" in out.missing


def test_target_price_above_threshold_returns_median():
    reports = [
        _report("2026-09-06 00:00:00.000", target=100.0),
        _report("2026-09-06 00:00:00.000", target=120.0),
        _report("2026-09-06 00:00:00.000", target=110.0),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF, target_price_coverage_threshold=0.3)

    assert out.values["target_price"] == 110.0
    assert "consensus_missing: target_price" not in out.missing


def test_string_fields_coerced():
    reports = [
        _report("2026-10-16 00:00:00.000", org=o, eps1="2.5", rating_value="3", last_rating_value="2") for o in ORGS
    ] + [
        _report("2026-10-16 00:00:00.000", org="机构D", eps1="", rating_value="", last_rating_value=""),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)

    assert out.values["eps_fy1"] == 2.5  # 空串被当作缺失，不参与中位数
    assert out.values["rating_mean"] == 3.0
    assert out.values["rating_upgrade_1m"] == 3  # 三家 3 vs 2
