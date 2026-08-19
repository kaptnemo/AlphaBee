"""对标组代码校验与规范化（COMPANY_TRACK Phase C4）。

- 代码规范化：``002415`` → ``002415.SZ``（按 6 位前缀推断交易所）；
  ``2382.TW`` 等境外代码保留交易所标注；
- A 股（SH/SZ/BJ）经 Tushare ``stock_basic`` 存在性校验（best-effort）；
- 境外代码**只进名单、不进基准计算**（避免跨市场财务口径错配）；
  无法识别交易所的候选直接剔除并告警。
"""

from __future__ import annotations

_DOMESTIC_EXCHANGES = ("SH", "SZ", "BJ")
# 常见境外交易所后缀（仅名单，不参与基准计算）
_INTERNATIONAL_EXCHANGES = ("TW", "HK", "US", "L", "T", "K", "O", "N", "SS", "KS")


def normalize_peer_code(code: str) -> tuple[str | None, str | None]:
    """对标组代码规范化 → ``(code, exchange)``；无法识别 → (None, None)。

    - ``002415.SZ`` → ("002415.SZ", "SZ")；
    - ``002415``（6 位数字）→ 按前缀推断交易所（6/9→SH，0/3→SZ，4/8→BJ）；
    - ``2382.TW`` → ("2382.TW", "TW")（境外，保留后缀）；
    - 其他（如 4 位无后缀）→ (None, None)。
    """
    raw = (code or "").strip().upper()
    if not raw:
        return None, None
    if "." in raw:
        digits, _, suffix = raw.partition(".")
        if suffix in _DOMESTIC_EXCHANGES and len(digits) == 6:
            return f"{digits}.{suffix}", suffix
        if suffix in _INTERNATIONAL_EXCHANGES:
            return raw, suffix
        return None, None
    if raw.isdigit() and len(raw) == 6:
        if raw[0] in "69":
            return f"{raw}.SH", "SH"
        if raw[0] in "03":
            return f"{raw}.SZ", "SZ"
        if raw[0] in "48":
            return f"{raw}.BJ", "BJ"
    return None, None


def split_domestic_international(
    candidates: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """候选拆分 → ``(domestic, international, invalid_names)``。

    domestic：A 股（进基准计算）；international：境外（仅名单）；
    invalid：代码无法识别交易所（剔除并告警）。
    """
    domestic: list[dict[str, str]] = []
    international: list[dict[str, str]] = []
    invalid: list[str] = []
    for cand in candidates:
        code, exchange = normalize_peer_code(cand.get("code", ""))
        if not code:
            invalid.append(cand.get("name") or cand.get("code") or "?")
            continue
        entry = {**cand, "code": code, "exchange": exchange}
        if exchange in _DOMESTIC_EXCHANGES:
            domestic.append(entry)
        else:
            international.append(entry)
    return domestic, international, invalid


def validate_a_share_codes(
    codes: list[str],
) -> tuple[list[str], list[str], str | None]:
    """A 股代码存在性校验（Tushare stock_basic，best-effort）。

    Returns:
        (valid, invalid, error)：tushare 不可用时按格式放行（error 非空说明降级）。
    """
    if not codes:
        return [], [], None
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper
    except Exception as exc:
        return codes, [], f"tushare 不可用，A股代码按格式放行: {exc}"

    valid: list[str] = []
    invalid: list[str] = []
    try:
        with TuShareHelper() as helper:
            for code in codes:
                try:
                    df = helper.stock_basic(ts_code=code, fields="ts_code").data
                    if df is not None and not df.empty:
                        valid.append(code)
                    else:
                        invalid.append(code)
                except Exception:
                    invalid.append(code)
        return valid, invalid, None
    except Exception as exc:
        return codes, [], f"tushare 校验不可用，A股代码按格式放行: {exc}"
