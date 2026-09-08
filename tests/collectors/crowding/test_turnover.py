"""crowding 交易热度纯函数单测（无网络）。

重点覆盖 review r1 C1：amount_pct_of_market 的非 None 路径——
get_market_fact 的 turnover_amount 位于 ``latest_daily`` 内（千元），需 ×1000 → 元，
再除以全市场成交额得 PERCENT。审查复现：正确 0.625% vs 旧实现 None。
"""

from __future__ import annotations

from alphabee.collectors.crowding.engine import compute_amount_pct_of_market
from alphabee.collectors.crowding.turnover import (
    _extract_turnover_amount_cny,
)


def test_extract_turnover_amount_reads_nested_latest_daily():
    # turnover_amount 位于 latest_daily（daily 日线行）内，单位千元
    mf = {"latest_daily": {"turnover_amount": 625}}
    assert _extract_turnover_amount_cny(mf) == 625000.0  # 625 千元 × 1000 = 625000 元


def test_extract_turnover_amount_missing_returns_none():
    assert _extract_turnover_amount_cny({}) is None
    assert _extract_turnover_amount_cny({"latest_daily": {}}) is None
    assert _extract_turnover_amount_cny(None) is None


def test_amount_pct_of_market_non_none_path():
    # 625 千元 / 1 亿元 → 625000 / 1e8 × 100 = 0.625%
    turnover_amount_cny = 625 * 1000.0
    market_turnover_cny = 1e8
    assert compute_amount_pct_of_market(turnover_amount_cny, market_turnover_cny) == 0.625


def test_amount_pct_of_market_missing_inputs_none():
    assert compute_amount_pct_of_market(None, 1e8) is None
    assert compute_amount_pct_of_market(625000.0, None) is None
    assert compute_amount_pct_of_market(625000.0, 0) is None
