"""Crowding (C 因子) collectors.

按 §6.4 优先级（免费/已有地基优先）落地 P0 数据基座：

- ``holders``  筹码集中度：AkShare ``stock_hold_num_cninfo`` → holder_count_change / per_capita_holding_change
- ``hot_rank`` 关注度：东财人气榜底层接口 → hot_rank（未上榜=缺失）
- ``coverage`` 关注度：复用东财研报数据 → analyst_coverage_rank（覆盖热度排名）
- ``turnover`` 交易热度：复用 turnover_rate/turnover_amount → turnover_rate_percentile / amount_pct_of_market

外部字段名只允许出现在本包（collector/fetcher 层），下游一律使用 canonical 名
（见 ``alphabee/schemas/crowding.yaml``）。缺失值显式 ``None``，绝不静默回退 0。
"""

from alphabee.collectors.crowding.collect import build_crowding
from alphabee.collectors.crowding.coverage import (
    fetch_analyst_coverage_rank,
    fetch_coverage_count,
)
from alphabee.collectors.crowding.engine import (
    CROWDING_P0_FIELDS,
    CrowdingOutput,
    compute_amount_pct_of_market,
    compute_turnover_rate_percentile,
    normalize_code,
    rank_by_coverage,
    sc_to_code,
)
from alphabee.collectors.crowding.holders import fetch_holder_changes
from alphabee.collectors.crowding.hot_rank import fetch_hot_rank, fetch_hot_rank_akshare
from alphabee.collectors.crowding.turnover import (
    fetch_amount_pct_of_market,
    fetch_turnover_rate_percentile,
)

__all__ = [
    "CROWDING_P0_FIELDS",
    "CrowdingOutput",
    "build_crowding",
    "compute_amount_pct_of_market",
    "compute_turnover_rate_percentile",
    "fetch_analyst_coverage_rank",
    "fetch_amount_pct_of_market",
    "fetch_coverage_count",
    "fetch_holder_changes",
    "fetch_hot_rank",
    "fetch_hot_rank_akshare",
    "fetch_turnover_rate_percentile",
    "normalize_code",
    "rank_by_coverage",
    "sc_to_code",
]
