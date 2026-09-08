"""crowding 引擎纯函数单测（无网络）——历史换手率分位 + 缺失值降级。

补 t13 要求的「聚合中位数/分位 + 缺失值降级」覆盖：
- compute_turnover_rate_percentile 的分位口径（历史中 ≤ current 的占比，0-1）；
- 历史观测数不足 min_obs 或 current 缺失/非正 → None（绝不静默回退 0）。
"""

from __future__ import annotations

from alphabee.collectors.crowding.engine import (
    MIN_PERCENTILE_OBSERVATIONS,
    compute_turnover_rate_percentile,
)


def test_percentile_rank_of_current():
    # 20 个历史观测 1..20，current=15 → 15/20 = 0.75
    history = [float(i) for i in range(1, 21)]
    assert compute_turnover_rate_percentile(history, 15.0) == 0.75


def test_percentile_ignores_missing_and_nonpositive():
    # 缺失/非正值观测不计入分母
    history = [None, 0.0, -1.0] + [float(i) for i in range(1, 21)]
    assert compute_turnover_rate_percentile(history, 15.0) == 0.75


def test_percentile_missing_when_insufficient_history():
    # 观测数 < min_obs → None（历史序列不足不硬凑）
    history = [float(i) for i in range(1, MIN_PERCENTILE_OBSERVATIONS)]  # 19 个
    assert compute_turnover_rate_percentile(history, 10.0) is None


def test_percentile_missing_when_current_invalid():
    history = [float(i) for i in range(1, 21)]
    assert compute_turnover_rate_percentile(history, None) is None
    assert compute_turnover_rate_percentile(history, 0.0) is None
    assert compute_turnover_rate_percentile(history, -1.0) is None


def test_percentile_custom_min_obs():
    # 自定义 min_obs：5 个观测即足够
    history = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert compute_turnover_rate_percentile(history, 3.0, min_obs=5) == 0.6
