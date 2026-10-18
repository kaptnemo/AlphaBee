"""A 股板块涨跌停幅度映射（P0 设计 §5，纯函数）。

按股票代码前缀判定板块涨跌停幅度；``is_st=True`` 时统一回落 ST 口径（5%）。
本模块为确定性纯函数：不调 LLM、不读外部状态、不发起 IO。

阈值/映射为**结构性示意，应回测**（P0 设计 §5）；未知前缀保守回落主板 10%。
"""

from __future__ import annotations

#: 板块涨跌停幅度（PERCENT）。BSE = 北交所（8/4/92 开头）。
BOARD_LIMITS: dict[str, float] = {
    "sh_main": 10.0,  # 600/601/603/605
    "sz_main": 10.0,  # 000/001/002/003
    "chinext": 20.0,  # 300/301 创业板
    "star": 20.0,  # 688/689 科创板
    "bse": 30.0,  # 北交所 8/4/92 开头
}

#: ST / *ST 涨跌停幅度（PERCENT）。
ST_LIMIT: float = 5.0

#: 未知前缀的保守回落（主板口径）。
_DEFAULT_LIMIT: float = 10.0


def _normalize_code(symbol: str) -> str:
    """股票代码归一：去交易所后缀 / 空白，保留数字主体（不校验位数）。"""
    code = (symbol or "").strip().upper()
    if "." in code:
        code = code.split(".")[0]
    return "".join(ch for ch in code if ch.isdigit())


def board_limit_pct(symbol: str, *, is_st: bool = False) -> float:
    """按代码前缀判定板块涨跌停幅度；``is_st=True`` → :data:`ST_LIMIT`（5%）。

    前缀规则（P0 设计 §5）：

    - 688/689 → 科创板 20%；
    - 300/301 → 创业板 20%；
    - 8/4/92 开头 → 北交所 30%；
    - 600/601/603/605 → 沪主板 10%；
    - 000/001/002/003 → 深主板 10%；
    - 未知前缀 → 10.0（主板口径，保守默认）。
    """
    if is_st:
        return ST_LIMIT
    code = _normalize_code(symbol)
    if code.startswith(("688", "689")):
        return BOARD_LIMITS["star"]
    if code.startswith(("300", "301")):
        return BOARD_LIMITS["chinext"]
    if code.startswith(("8", "4", "92")):
        return BOARD_LIMITS["bse"]
    if code.startswith(("600", "601", "603", "605")):
        return BOARD_LIMITS["sh_main"]
    if code.startswith(("000", "001", "002", "003")):
        return BOARD_LIMITS["sz_main"]
    return _DEFAULT_LIMIT


def limit_ratio(change_pct: float, symbol: str, *, is_st: bool = False) -> float | None:
    """|change_pct| / board_limit_pct（0-1+）；封板判定阈值由调用方定。

    板块涨跌停幅度非法（<=0）时返回 ``None``（不猜测）。
    """
    limit = board_limit_pct(symbol, is_st=is_st)
    if limit <= 0.0:
        return None
    return abs(float(change_pct)) / limit
