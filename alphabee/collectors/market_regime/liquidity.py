"""Collector: interest rates & liquidity (bond yields, SHIBOR, M1/M2, social financing).

Sources (akshare):
- ``bond_china_yield`` — 中债国债收益率曲线（10年）
- ``bond_zh_us_rate`` — 中美国债收益率（10年）
- ``macro_china_shibor_all`` — SHIBOR 报价（3M）
- ``macro_china_money_supply`` — M1/M2 同比增速
- ``macro_china_shrzgm`` — 社会融资规模增量
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from alphabee.collectors.market_regime._utils import month_key, select_latest
from alphabee.market_regime.models import CollectorOutput

SOURCE_BOND = "akshare:bond_china_yield/bond_zh_us_rate"
SOURCE_SHIBOR = "akshare:macro_china_shibor_all"
SOURCE_MONEY = "akshare:macro_china_money_supply"
SOURCE_SF = "akshare:macro_china_shrzgm"


def _get_ak(ak_module: Any = None) -> Any:
    if ak_module is not None:
        return ak_module
    import akshare as ak  # noqa: PLC0415

    return ak


def m1_m2_gap(m1_yoy: float | None, m2_yoy: float | None) -> float | None:
    """M1-M2 剪刀差（%）= M1同比 - M2同比。"""
    if m1_yoy is None or m2_yoy is None:
        return None
    return round(m1_yoy - m2_yoy, 2)


def _month_gap(later: str, earlier: str) -> int:
    """Whole-month gap between two ``YYYYMM`` strings (later minus earlier)."""
    ly, lm = int(later[:4]), int(later[4:6])
    ey, em = int(earlier[:4]), int(earlier[4:6])
    return (ly * 12 + lm) - (ey * 12 + em)


def _asof_month(asof_date: str | None) -> str:
    """Normalize an asof date (``YYYY-MM-DD``) to a ``YYYYMM`` month key."""
    if not asof_date:
        return ""
    digits = "".join(ch for ch in str(asof_date) if ch.isdigit())
    return digits[:6]


def _best_latest_month(
    fetch_fn: Callable[[], pd.DataFrame | None],
    key_col: str,
    asof_month: str | None,
    attempts: int = 3,
) -> pd.DataFrame | None:
    """Fetch a monthly series, retrying until the newest month is captured.

    外部月频接口偶发返回"缺最新月份"的部分数据，因此重复拉取并保留
    ``_month`` 最大的那次结果。
    """
    best: pd.DataFrame | None = None
    best_month: str | None = None
    for _ in range(attempts):
        try:
            raw = fetch_fn()
        except Exception:  # noqa: BLE001
            continue
        if raw is None or raw.empty:
            continue
        frame = raw.copy()
        frame["_month"] = frame[key_col].map(month_key)
        frame = frame[frame["_month"].notna()]
        if frame.empty:
            continue
        current_month = frame["_month"].max()
        if best is None or current_month > (best_month or ""):
            best, best_month = frame, current_month
        if asof_month and current_month >= asof_month:
            break
    return best


def _parse_cn_bond_yield(df: pd.DataFrame, column: str, asof_date: str | None) -> float | None:
    """Parse 10y yield from bond_china_yield, restricting to 中债国债收益率曲线 rows."""
    if df is None or df.empty or column not in df.columns:
        return None
    curve_rows = df
    if "曲线名称" in df.columns:
        curve_rows = df[df["曲线名称"] == "中债国债收益率曲线"]
        if curve_rows.empty:
            return None
    row = select_latest(curve_rows, "日期", asof_date)
    if row is None or pd.isna(row[column]):
        return None
    return float(row[column])


def _parse_us_bond_yield(df: pd.DataFrame, asof_date: str | None) -> float | None:
    """Parse US 10y yield from bond_zh_us_rate."""
    row = select_latest(df, "日期", asof_date)
    if row is None or pd.isna(row.get("美国国债收益率10年")):
        return None
    return float(row["美国国债收益率10年"])


def fetch_bond_yields(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """China 10y treasury yield and US 10y treasury yield (full-history source)."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        us_rate = ak.bond_zh_us_rate()
        row = select_latest(us_rate, "日期", asof_date)
        if row is not None:
            if pd.notna(row.get("中国国债收益率10年")):
                values["cn_10y_yield"] = float(row["中国国债收益率10年"])
            if pd.notna(row.get("美国国债收益率10年")):
                values["us_10y_yield"] = float(row["美国国债收益率10年"])
        if "cn_10y_yield" not in values:
            warnings.append("中债10年收益率缺失（数据源该日为 NaN）")
        if "us_10y_yield" not in values:
            warnings.append("美债10年收益率缺失（数据源该日为 NaN）")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"中美收益率获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_BOND, warnings=warnings)


def fetch_shibor(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """3-month SHIBOR quote."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}

    try:
        shibor = ak.macro_china_shibor_all()
        row = select_latest(shibor, "日期", asof_date)
        if row is not None and pd.notna(row.get("3M-定价")):
            values["shibor_3m"] = float(row["3M-定价"])
    except Exception:  # noqa: BLE001
        pass

    return CollectorOutput(values=values, source=SOURCE_SHIBOR)


def fetch_money_supply(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """M1 / M2 YoY growth and the derived M1-M2 gap (monthly, latest month ≤ asof)."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []
    asof_month = _asof_month(asof_date)

    try:
        money = _best_latest_month(
            ak.macro_china_money_supply,
            "月份",
            asof_month,
        )
        if money is not None:
            candidates = money.sort_values("_month")
            if asof_month:
                candidates = candidates[candidates["_month"] <= asof_month]
            if not candidates.empty:
                row = candidates.iloc[-1]
                if pd.notna(row.get("货币(M1)-同比增长")):
                    values["m1_yoy"] = float(row["货币(M1)-同比增长"])
                if pd.notna(row.get("货币和准货币(M2)-同比增长")):
                    values["m2_yoy"] = float(row["货币和准货币(M2)-同比增长"])
                gap = m1_m2_gap(values.get("m1_yoy"), values.get("m2_yoy"))
                if gap is not None:
                    values["m1_m2_gap"] = gap
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"M1/M2 获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_MONEY, warnings=warnings)


def fetch_social_financing(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """社会融资规模当月增量（亿元），月度最新一期。"""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []
    asof_month = _asof_month(asof_date)

    try:
        sf = _best_latest_month(
            ak.macro_china_shrzgm,
            "月份",
            asof_month,
        )
        if sf is not None:
            candidates = sf.sort_values("_month")
            if asof_month:
                candidates = candidates[candidates["_month"] <= asof_month]
            if not candidates.empty:
                row = candidates.iloc[-1]
                selected_month = row["_month"]
                if asof_month and len(selected_month) == 6:
                    months_behind = _month_gap(asof_month, selected_month)
                    if months_behind > 3:
                        warnings.append(
                            f"社融增量最新月份为 {selected_month}，落后 asof_month={asof_month[:6]} 超过3个月"
                        )
                if pd.notna(row.get("社会融资规模增量")):
                    values["social_financing_increment"] = float(row["社会融资规模增量"])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"社融增量获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_SF, warnings=warnings)


def fetch(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """Merge all liquidity outputs into one collector result."""
    merged = CollectorOutput(source=f"{SOURCE_BOND}/{SOURCE_SHIBOR}/{SOURCE_MONEY}/{SOURCE_SF}")
    for collector in (fetch_bond_yields, fetch_shibor, fetch_money_supply, fetch_social_financing):
        part = collector(asof_date, ak_module=ak_module)
        merged.values.update(part.values)
        merged.warnings.extend(part.warnings)
    return merged


def history(
    start: str | None = None,
    end: str | None = None,
    *,
    ak_module: Any = None,
) -> pd.DataFrame:
    """Full historical liquidity series as a date-indexed DataFrame of canonical fields.

    月频字段（M1/M2、社融）按月数据发布时点落位，并在日频网格上做 forward-fill，
    使下游无需自行处理频率错配。债券/SHIBOR 为日频。
    """
    ak = _get_ak(ak_module)
    frames: dict[str, pd.Series] = {}

    try:
        us_rate = ak.bond_zh_us_rate()
        us_rate = us_rate.set_index(pd.to_datetime(us_rate["日期"]))
        if "中国国债收益率10年" in us_rate.columns:
            frames["cn_10y_yield"] = pd.to_numeric(us_rate["中国国债收益率10年"], errors="coerce")
        if "美国国债收益率10年" in us_rate.columns:
            frames["us_10y_yield"] = pd.to_numeric(us_rate["美国国债收益率10年"], errors="coerce")
    except Exception:  # noqa: BLE001
        pass

    try:
        shibor = ak.macro_china_shibor_all()
        shibor = shibor.set_index(pd.to_datetime(shibor["日期"]))
        if "3M-定价" in shibor.columns:
            frames["shibor_3m"] = pd.to_numeric(shibor["3M-定价"], errors="coerce")
    except Exception:  # noqa: BLE001
        pass

    monthly: dict[str, pd.Series] = {}
    try:
        money = ak.macro_china_money_supply()
        money = money.copy()
        money["_month"] = money["月份"].map(month_key)
        money = money[money["_month"].notna()].sort_values("_month")
        month_index = pd.to_datetime(money["_month"], format="%Y%m")
        if "货币(M1)-同比增长" in money.columns:
            m1 = pd.to_numeric(money["货币(M1)-同比增长"], errors="coerce")
            m2 = pd.to_numeric(money["货币和准货币(M2)-同比增长"], errors="coerce")
            monthly["m1_yoy"] = pd.Series(m1.values, index=month_index)
            monthly["m2_yoy"] = pd.Series(m2.values, index=month_index)
            monthly["m1_m2_gap"] = monthly["m1_yoy"] - monthly["m2_yoy"]
    except Exception:  # noqa: BLE001
        pass

    try:
        sf = ak.macro_china_shrzgm()
        sf = sf.copy()
        sf["_month"] = sf["月份"].map(month_key)
        sf = sf[sf["_month"].notna()].sort_values("_month")
        month_index = pd.to_datetime(sf["_month"], format="%Y%m")
        monthly["social_financing_increment"] = pd.Series(
            pd.to_numeric(sf["社会融资规模增量"], errors="coerce").values,
            index=month_index,
        )
    except Exception:  # noqa: BLE001
        pass

    daily = pd.DataFrame(frames).sort_index()
    if daily.empty and not monthly:
        return pd.DataFrame()

    # 月频序列合并到日频网格：按月时间点落位，再 forward-fill 到日频
    for name, series in monthly.items():
        s = series.sort_index()
        if daily.empty:
            daily = s.to_frame(name=name)
        else:
            daily[name] = s.reindex(daily.index, method="ffill")

    daily = daily.sort_index()
    if start:
        daily = daily[daily.index >= pd.to_datetime(start)]
    if end:
        daily = daily[daily.index <= pd.to_datetime(end)]
    return daily.round(4)
