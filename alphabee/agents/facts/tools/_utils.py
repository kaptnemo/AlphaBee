"""Shared utilities for FactCollectorAgent tools."""

import math


def normalize_ts_code(symbol: str) -> str:
    """Convert various stock symbol formats to Tushare standard format."""
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:].upper() + ".SH"
    if s.startswith("sz"):
        return s[2:].upper() + ".SZ"
    if s.startswith("bj"):
        return s[2:].upper() + ".BJ"
    upper = symbol.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return upper + ".SH"
    if upper.startswith(("0", "3")):
        return upper + ".SZ"
    if upper.startswith(("4", "8")):
        return upper + ".BJ"
    raise ValueError(f"Cannot determine exchange for symbol: {symbol}")


def to_pure_code(ts_code: str) -> str:
    """Extract the 6-digit code from a Tushare code, e.g. '600519.SH' -> '600519'."""
    return ts_code.split(".")[0]


def safe_float(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def safe_str(value, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return default if s in ("nan", "None", "") else s
