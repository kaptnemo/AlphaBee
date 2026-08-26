"""Collector: market breadth (advance/decline breadth).

Source (akshare): ``stock_market_activity_legu`` — 全市场当日涨跌/涨停/跌停快照。

设计说明：MA60 宽度（breadth_above_ma60_pct）与 NH-NL（nh_nl_diff）需要全市场
个股历史日线，Phase 0 无低成本数据源，已登记为 schema field_gaps；本模块只
产出可低成本获得的 `up_stock_ratio` / `limit_up_count` / `limit_down_count`。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from alphabee.collectors.market_regime._utils import _coerce_date
from alphabee.market_regime.models import CollectorOutput

SOURCE = "akshare:stock_market_activity_legu"


def _get_ak(ak_module: Any = None) -> Any:
    if ak_module is not None:
        return ak_module
    import akshare as ak  # noqa: PLC0415

    return ak


def parse_activity(df: pd.DataFrame) -> tuple[dict[str, float], str | None, list[str]]:
    """Parse the legu activity snapshot into ``(values, stat_date, warnings)``.

    ``values`` contains canonical fields only: ``up_stock_ratio`` (%), plus
    ``limit_up_count`` / ``limit_down_count`` when available.
    """
    if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
        return {}, None, ["市场活跃度数据为空"]

    items: dict[str, float] = {}
    stat_date: str | None = None
    for _, row in df.iterrows():
        name = str(row["item"])
        if name == "统计日期":
            stat_date = str(row["value"]).strip() if row["value"] is not None else None
            continue
        try:
            items[name] = float(row["value"])
        except (TypeError, ValueError):
            continue

    values: dict[str, float] = {}
    warnings: list[str] = []

    up = items.get("上涨")
    down = items.get("下跌")
    flat = items.get("平盘")
    if up is not None and down is not None and flat is not None:
        total = up + down + flat
        if total > 0:
            values["up_stock_ratio"] = round(up / total * 100, 2)
    else:
        warnings.append("上涨/下跌/平盘家数缺失，无法计算 up_stock_ratio")

    for key, field in (("涨停", "limit_up_count"), ("跌停", "limit_down_count")):
        value = items.get(key)
        if value is not None:
            values[field] = value

    return values, stat_date, warnings


def fetch(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """Latest A-share breadth snapshot (source is current-only, no history)."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        activity = ak.stock_market_activity_legu()
        values, stat_date, parse_warnings = parse_activity(activity)
        warnings.extend(parse_warnings)
        if stat_date and asof_date:
            stat_day = _coerce_date(stat_date)
            asof_day = _coerce_date(asof_date)
            if stat_day is not None and asof_day is not None and stat_day != asof_day:
                warnings.append(f"活跃度数据日为 {stat_day}，晚于 asof_date={asof_day}")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"市场活跃度获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE, warnings=warnings)
