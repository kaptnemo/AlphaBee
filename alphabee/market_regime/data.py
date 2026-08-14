"""Market-regime data orchestration: normalize external data into snapshots.

``collect_snapshot`` merges every collector into a single dated
``MarketIndicatorSnapshot`` (canonical fields only). ``backfill_history`` replays
full historical series (valuation + liquidity) into the daily CSV so percentile
windows (ERP / PE / PB 分位) have history to draw on in Phase 1.

数据源策略（``source``）：
- ``"auto"``（默认）：tushare 优先，缺失字段由 akshare 补齐（如中债收益率、
  市场宽度、全A估值分位、两市成交额）；tushare 提供的字段（创业板估值、融资余额、
  社融增量等）覆盖 akshare。
- ``"tushare"`` / ``"akshare"``：强制只用单一数据源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from alphabee.collectors.market_regime import (
    breadth,
    index_valuation,
    liquidity,
    risk_preference,
    tushare,
)
from alphabee.market_regime.models import CollectorOutput, MarketIndicatorSnapshot
from alphabee.market_regime.persistence import append_snapshot

# ── 采集器注册表 ─────────────────────────────────────────────────────────
#
# 业务背景：市场状态快照由四大主题组成——
#   valuation        估值（沪深300 PE/PB、EPR 股债利差、创业板估值）
#   liquidity        流动性（利率周期、M1/M2、社融拐点）
#   breadth          市场宽度（站上60日线个股占比、上涨家数占比）→ 趋势引擎用
#   risk_preference  风险偏好（成交额、融资余额、ETF 资金流）→ 仅作 ±5 调整项
#
# akshare 与 tushare 的数据覆盖差异（决定 auto 模式的合并优先级）：
#   - breadth 只有 akshare 有：tushare 不提供全市场个股站上均线的广度统计，
#     因此 TUSHARE_COLLECTORS 里没有 breadth 主题。
#   - 对重叠主题（valuation/liquidity/risk_preference），auto 模式让 tushare
#     后 merge 覆盖 akshare（见 collect_snapshot），因为 tushare 的字段口径更
#     权威（如沪深300 PE/PB、融资余额、社融、M1/M2）。

# 各主题下 akshare 采集器（breadth 仅有 akshare 数据源）
AKSHARE_COLLECTORS: dict[str, Any] = {
    "valuation": index_valuation.fetch,
    "liquidity": liquidity.fetch,
    "breadth": breadth.fetch,
    "risk_preference": risk_preference.fetch,
}

# 各主题下 tushare 采集器（无 breadth）
TUSHARE_COLLECTORS: dict[str, Any] = {
    "valuation": tushare.fetch_index_valuation,
    "liquidity": tushare.fetch_liquidity,
    "risk_preference": tushare.fetch_margin,
}

# 四主题的遍历顺序：估值 → 流动性 → 宽度 → 风险偏好。
# 顺序本身不影响结果（各主题写入独立的 canonical 字段），仅用于日志/迭代确定性。
ALL_THEMES = ("valuation", "liquidity", "breadth", "risk_preference")


def collect_snapshot(
    asof_date: str | None = None,
    *,
    enabled: list[str] | None = None,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> MarketIndicatorSnapshot:
    """Collect a single dated market snapshot.

    Args:
        asof_date:  ``YYYY-MM-DD``; default is the latest available data date.
        enabled:    subset of collector themes to run; default runs all.
        source:     ``"auto"`` | ``"tushare"`` | ``"akshare"``.
        ak_module:  injectable akshare-like module for testing.
        ts_module:  injectable tushare pro_api-like module for testing.

    Returns:
        A ``MarketIndicatorSnapshot`` with merged canonical values and provenance.
        With ``source="auto"``, tushare values win over akshare for overlapping
        fields; akshare fills fields tushare cannot serve.
    """
    if source not in ("auto", "tushare", "akshare"):
        raise ValueError(f"未知数据源: {source!r}（可选 auto/tushare/akshare）")

    # 日期语义：asof_date 缺省时用"当天自然日"。市场状态快照是日频的，
    # 采集端允许获取"最近一个交易日"的数据（akshare/tushare 无参即取最新日），
    # 因此这里用当天兜底即可；真正的交易日对齐由各采集器内部负责。
    snapshot = MarketIndicatorSnapshot(
        date=asof_date or datetime.now(UTC).date().isoformat(),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    # enabled 用于局部刷新（如只补估值），缺省跑全部四主题保证快照字段完整。
    selected = set(enabled) if enabled else set(ALL_THEMES)
    use_tushare = source in ("auto", "tushare")
    use_akshare = source in ("auto", "akshare")

    for theme in ALL_THEMES:
        if theme not in selected:
            continue
        # 关键顺序：akshare 先 merge、tushare 后 merge。
        # MarketIndicatorSnapshot.merge 采用"后写覆盖"语义，因此 auto 模式下
        # 重叠字段一律以 tushare 为准（其数据口径更权威），akshare 只负责补齐
        # tushare 无法提供的字段（中债收益率、市场宽度、全A估值分位等）。
        if use_akshare and theme in AKSHARE_COLLECTORS:
            output: CollectorOutput = AKSHARE_COLLECTORS[theme](asof_date, ak_module=ak_module)
            snapshot.merge(output)
        if use_tushare and theme in TUSHARE_COLLECTORS:
            output = TUSHARE_COLLECTORS[theme](asof_date, ts_module=ts_module)
            snapshot.merge(output)
    return snapshot


def collect_and_persist(
    asof_date: str | None = None,
    path: str | None = None,
    *,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> MarketIndicatorSnapshot:
    """Collect a snapshot and append it to the daily indicator CSV (idempotent by date)."""
    snapshot = collect_snapshot(asof_date, source=source, ak_module=ak_module, ts_module=ts_module)
    append_snapshot(snapshot, path=path)
    return snapshot


def backfill_history(
    start: str,
    end: str | None = None,
    path: str | None = None,
    *,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> int:
    """Backfill historical valuation + liquidity series into the daily CSV.

    With ``source="auto"`` the tushare history (per-index PE/PB incl. 创业板,
    SHIBOR, US yield, M1/M2, 社融) is combined with akshare history (中债收益率、
    全A估值分位); tushare wins on overlapping columns.

    Returns the number of rows written.
    """
    use_tushare = source in ("auto", "tushare")
    use_akshare = source in ("auto", "akshare")

    frames: list[pd.DataFrame] = []
    if use_akshare:
        frames.append(index_valuation.history(start, end, ak_module=ak_module))
        frames.append(liquidity.history(start, end, ak_module=ak_module))
    if use_tushare:
        frames.append(tushare.history(start, end, ts_module=ts_module))

    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return 0

    # 历史合并策略：以第一个 frame 为底，后续 frame 用 combine_first 填补缺失列。
    # combine_first 只填"底表中没有的列"，不会覆盖底表已有的值，因此：
    #   - tushare 排在最后 → tushare 提供的列优先级最高，保留其值；
    #   - akshare 只补充 tushare 没有的中债收益率、全A估值分位等列。
    # 这保证回填的历史序列口径与 collect_snapshot 的 auto 优先级完全一致。
    merged = frames[0]
    for frame in frames[1:]:
        # 后加入的 frame 覆盖同名列（tushare 放在最后 → 优先级最高）
        merged = frame.combine_first(merged)
    # 按日期索引排序，形成连续时间序列，供后续 percentile/动量窗口切片使用。
    merged = merged.sort_index()

    written = 0
    for idx, row in merged.iterrows():
        values = {name: float(value) for name, value in row.items() if pd.notna(value)}
        if not values:
            continue
        snapshot = MarketIndicatorSnapshot(
            date=idx.date().isoformat(),
            values=values,
            sources={name: "backfill" for name in values},
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        append_snapshot(snapshot, path=path)
        written += 1
    return written
