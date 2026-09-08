"""consensus 聚合引擎单测（纯函数，无网络）。

重点覆盖 DATA_CONTRACTS_VERIFIED.md §1 的契约修正：
- emRatingValue 方向（持有1/增持2/买入3，越大越看多）→ rating_mean 均值。
- 评级方向差分用 emRatingValue vs lastEmRatingValue，禁止读 ratingChange。
- 目标价覆盖率低 → target_price=None + consensus_missing: target_price。
"""

from __future__ import annotations

from alphabee.collectors.consensus.engine import (
    CONSENSUS_FIELDS,
    aggregate_consensus,
)

ASOF = "2026-09-06"


def _report(
    publish_date: str,
    *,
    eps1=None,
    eps2=None,
    eps3=None,
    target=None,
    rating_value=None,
    last_rating_value=None,
    rating_change=None,
    org="机构A",
):
    return {
        "publishDate": publish_date,
        "predictThisYearEps": eps1,
        "predictNextYearEps": eps2,
        "predictNextTwoYearEps": eps3,
        "indvAimPriceT": target,
        "emRatingValue": rating_value,
        "lastEmRatingValue": last_rating_value,
        "ratingChange": rating_change,
        "orgSName": org,
    }


# ─────────────────────────────────────────────────────────────────────────────
def test_eps_median_uses_latest_day():
    reports = [
        _report("2026-09-05 00:00:00.000", eps1=2.0),
        _report("2026-09-06 00:00:00.000", eps1=2.0),
        _report("2026-09-06 00:00:00.000", eps1=2.2),
        _report("2026-09-06 00:00:00.000", eps1=2.4),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert out.values["eps_fy1"] == 2.2  # 最新一日中位数
    assert out.values["eps_fy2"] is None
    assert "consensus_missing: eps_fy2" in out.missing


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
        _report("2026-09-01 00:00:00.000", rating_value=3, last_rating_value=2),
        # 下调：1 vs 2
        _report("2026-09-02 00:00:00.000", rating_value=1, last_rating_value=2),
        # ratingChange=1 但 emRatingValue==lastEmRatingValue → 不算上调（ratingChange 被忽略）
        _report(
            "2026-09-03 00:00:00.000",
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


def test_eps_revision_1m():
    reports = [
        _report("2026-08-07 00:00:00.000", eps1=1.0),
        _report("2026-09-06 00:00:00.000", eps1=1.1),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert out.values["eps_fy1_revision_1m"] == 10.0  # (1.1/1.0 - 1) * 100


def test_coverage_count_distinct_orgs():
    reports = [
        _report("2026-09-06 00:00:00.000", org="机构A"),
        _report("2026-09-06 00:00:00.000", org="机构A"),
        _report("2026-09-06 00:00:00.000", org="机构B"),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert out.values["coverage_count"] == 2


def test_empty_reports_all_missing():
    out = aggregate_consensus([], as_of_date=ASOF)
    assert out.values["coverage_count"] is None
    for f in CONSENSUS_FIELDS:
        assert out.values[f] is None
    assert all(f"consensus_missing: {f}" in out.missing for f in CONSENSUS_FIELDS)


def test_string_fields_coerced():
    reports = [
        _report("2026-09-06 00:00:00.000", eps1="2.5", rating_value="3", last_rating_value="2"),
        _report("2026-09-06 00:00:00.000", eps1="", rating_value="", last_rating_value=""),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert out.values["eps_fy1"] == 2.5  # 空串被当作缺失，不参与中位数
    assert out.values["rating_mean"] == 3.0
    assert out.values["rating_upgrade_1m"] == 1  # 3 vs 2


def test_revision_acceleration_second_order_diff():
    # revision_acceleration = revision_1m 的二阶差分（相邻两个 revision_1m 之差）
    # 07-01→08-01 上修 10%；08-01→09-01 上修 4.5455% → 加速度 = 4.5455 - 10 = -5.4545
    reports = [
        _report("2026-07-01 00:00:00.000", eps1=1.0),
        _report("2026-08-01 00:00:00.000", eps1=1.1),
        _report("2026-09-01 00:00:00.000", eps1=1.15),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert abs(out.values["revision_acceleration"] - (-5.4545)) < 1e-4


def test_revision_acceleration_missing_with_single_point():
    # 仅一个可计算 revision_1m 的时点 → 二阶差分无法计算 → None + consensus_missing
    reports = [
        _report("2026-08-01 00:00:00.000", eps1=1.0),
        _report("2026-09-01 00:00:00.000", eps1=1.1),
    ]
    out = aggregate_consensus(reports, as_of_date=ASOF)
    assert out.values["revision_acceleration"] is None
    assert "consensus_missing: revision_acceleration" in out.missing
