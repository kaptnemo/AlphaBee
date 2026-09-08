"""Crowding (C 因子) collector — pure computation helpers（无网络 I/O）。

本模块是 *source-specific collector* 的一部分：允许接触外部字段名的读取只在各
fetcher 模块内发生；这里只做纯计算，输出 canonical 字段（见
``alphabee/schemas/crowding.yaml``）。缺失值一律 ``None``，绝不静默回退 0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# P0 优先落地字段（免费/已有地基）：筹码集中度 + 关注度 + 交易热度
CROWDING_P0_FIELDS: tuple[str, ...] = (
    "holder_count_change",
    "per_capita_holding_change",
    "hot_rank",
    "analyst_coverage_rank",
    "turnover_rate_percentile",
    "amount_pct_of_market",
)

# 默认历史分位所需最小观测数（低于阈值 → None，历史序列不足不硬凑）
MIN_PERCENTILE_OBSERVATIONS = 20


@dataclass
class CrowdingOutput:
    """一次个股拥挤度采集结果。

    - ``values``：canonical 字段 → 值（``None`` 表示缺失）。
    - ``missing``：显式缺失 issue 列表，形如 ``crowding_missing: hot_rank``。
    - ``warnings``：非致命提示。
    """

    values: dict[str, float | int | None] = field(default_factory=dict)
    source: str = ""
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    as_of_date: str = ""


def normalize_code(code: str) -> str:
    """``300750.SZ`` / ``sz300750`` → ``300750``（6 位，去交易所前后缀）。"""
    code = (code or "").strip().lower()
    if code.startswith(("sz", "sh", "bj")):
        code = code[2:]
    if "." in code:
        code = code.split(".")[0]
    return code


def sc_to_code(sc: str) -> str:
    """东财人气榜 ``sc``（如 ``SZ000592``）→ 6 位纯代码 ``000592``。"""
    sc = (sc or "").strip().upper()
    if len(sc) >= 8 and sc[:2] in ("SZ", "SH", "BJ"):
        return sc[2:8]
    return sc[-6:] if len(sc) >= 6 else sc


def to_float(value: Any) -> float | None:
    """str/int/float/空串 → float；空串/无效值 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN", "null"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    v = to_float(value)
    return int(v) if v is not None else None


def compute_turnover_rate_percentile(
    history: list[float | None],
    current: float | None,
    min_obs: int = MIN_PERCENTILE_OBSERVATIONS,
) -> float | None:
    """当前换手率在历史序列中的分位（0-1，历史中 ≤ current 的占比）。

    仅统计正观测；历史观测数 < ``min_obs`` 或 current 缺失/非正 → None（不静默回退 0）。
    """
    vals = [v for v in history if v is not None and v > 0]
    if current is None or current <= 0 or len(vals) < min_obs:
        return None
    below_or_equal = sum(1 for v in vals if v <= current)
    return round(below_or_equal / len(vals), 4)


def compute_amount_pct_of_market(
    turnover_amount_cny: float | None,
    market_turnover_cny: float | None,
) -> float | None:
    """个股成交额占全市场成交额比（PERCENT）。

    两个入参必须同一单位（元，CNY）；调用方负责把市场成交额从亿元换算为元。
    任一缺失或分母非正 → None。
    """
    if turnover_amount_cny is None or market_turnover_cny is None or market_turnover_cny <= 0:
        return None
    return round(turnover_amount_cny / market_turnover_cny * 100.0, 4)


def rank_by_coverage(counts: dict[str, int | None], code: str) -> int | None:
    """按覆盖研报/机构数降序排名，返回 ``code`` 的排名（1 = 最热）。

    ``code`` 无覆盖（None/0）→ None（无覆盖=缺失）。
    """
    target = normalize_code(code)
    items = [(normalize_code(c), n) for c, n in counts.items()]
    ranked = sorted(items, key=lambda x: (-(x[1] or 0), x[0]))
    for i, (c, n) in enumerate(ranked, start=1):
        if c == target:
            return i if (n is not None and n > 0) else None
    return None
