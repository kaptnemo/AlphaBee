"""Crowding 采集器 — 筹码集中度（股东户数 / 人均持股）。

数据源：AkShare ``stock_hold_num_cninfo(date="YYYYMMDD")``（巨潮，全市场快照）。
契约（docs/midterm/DATA_CONTRACTS_VERIFIED.md §2）：
- 参数是 ``date``（非 symbol），必须季度末披露日（0331/0630/0930/1231）；
  非季度末日期抛 ``KeyError('records')``，调用方需 try/except 并回退到更早季度末。
- 输出列「股东人数增幅」「人均持股数量增幅」已是百分比数值（如 -1.79 = -1.79%），
  映射到 canonical 后不得再乘 100。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from alphabee.collectors.crowding.engine import normalize_code, to_float

# 巨潮股东户数原始列名（外部名只允许出现在本 fetcher 层）
_COL_CODE = "证券代码"
_COL_HOLDER_CHANGE = "股东人数增幅"
_COL_PER_CAPITA_CHANGE = "人均持股数量增幅"


def _get_akshare(ak_module: Any = None) -> Any:
    if ak_module is not None:
        return ak_module
    import akshare as ak  # noqa: PLC0415

    return ak


def _quarter_end_dates(max_years: int = 5) -> list[str]:
    """生成从今天往前最多 ``max_years`` 个季度末披露日（YYYYMMDD，降序）。"""
    today = date.today()
    out: list[str] = []
    for year in range(today.year, today.year - max_years - 1, -1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            d = date(year, month, day)
            if d <= today:
                out.append(d.strftime("%Y%m%d"))
    return out


def fetch_holder_changes(
    code: str,
    *,
    date_: str | None = None,
    ak_module: Any = None,
) -> tuple[float | None, float | None]:
    """采股东户数/人均持股增幅 → ``(holder_count_change, per_capita_holding_change)``。

    Args:
        code: 6 位股票代码（支持 ``300750.SZ``）。
        date_: 季度末披露日（``YYYYMMDD``）；缺省自动回退到最近的季度末。
        ak_module: 测试注入的 akshare 模块。

    Returns:
        ``(holder_count_change, per_capita_holding_change)``，均为 PERCENT 数值；
        无该股票记录或接口失败 → ``(None, None)``（不静默回退 0）。
    """
    ak = _get_akshare(ak_module)
    sec_code = normalize_code(code)
    if not sec_code:
        return None, None

    candidates = [date_] if date_ else _quarter_end_dates()
    for d in candidates:
        if not d:
            continue
        try:
            df = ak.stock_hold_num_cninfo(date=d)
        except Exception:
            continue
        if df is None or getattr(df, "empty", True) or _COL_CODE not in df.columns:
            continue
        rows = df[df[_COL_CODE].astype(str).str.strip() == sec_code]
        if rows.empty:
            continue
        row = rows.iloc[0]
        return to_float(row.get(_COL_HOLDER_CHANGE)), to_float(row.get(_COL_PER_CAPITA_CHANGE))
    return None, None
