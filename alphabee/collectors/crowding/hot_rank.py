"""Crowding 采集器 — 关注度：东财人气榜 top100（hot_rank）。

契约（docs/midterm/DATA_CONTRACTS_VERIFIED.md §3，实测）：
- ``stock_hot_rank_em()`` 完整函数在沙箱抛 ProxyError（push2.eastmoney.com 不可达），
  但其第一步底层榜单接口 ``emappdata.eastmoney.com/stockrank/getAllCurrentList`` 可达。
- 底层接口 POST 返回 100 行 ``{sc(如 "SZ000592"), rk(1-100 排名), rc, hisRc}``，
  仅 top100（pageNo=1&pageSize=100 有效，>100 或 >1 页均 0 行）。
- 因此 ``fetch_hot_rank`` 直连底层接口（只需排名+代码）；``fetch_hot_rank_akshare``
  保留 ``stock_hot_rank_em()`` 作为生产环境（push2 可达）的兜底。
- 未上榜 → ``None``（显式缺失，绝不回退 0）。
"""

from __future__ import annotations

from typing import Any

from alphabee.collectors.crowding.engine import normalize_code, sc_to_code, to_int

# 请求常量以 docs/midterm/DATA_CONTRACTS_VERIFIED.md §3「底层接口请求体与响应信封」为准
# （engineer-e 2026-09-06 复验 HTTP 200；响应信封为 {globalId,message,status,code,data,stack}，
#   data[] 每行 {sc, rk, rc, hisRc}，仅 top100）。
EMAPP_HOT_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EMAPP_HOT_RANK_PAYLOAD: dict[str, Any] = {
    "appId": "appId01",
    "globalId": "786e4c21-70dc-435a-93bb-38",
    "marketType": "",
    "pageNo": 1,
    "pageSize": 100,
}


def fetch_hot_rank(code: str, *, timeout: int = 20) -> int | None:
    """直连东财人气榜底层接口，返回 ``code`` 的人气榜排名（1-100）；未上榜 → None。"""
    import requests  # noqa: PLC0415

    sec_code = normalize_code(code)
    if not sec_code:
        return None
    try:
        resp = requests.post(EMAPP_HOT_RANK_URL, json=EMAPP_HOT_RANK_PAYLOAD, timeout=timeout)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or []
    except Exception:
        return None
    for row in data:
        if not isinstance(row, dict):
            continue
        sc = row.get("sc")
        if isinstance(sc, str) and sc_to_code(sc) == sec_code:
            return to_int(row.get("rk"))
    return None


def fetch_hot_rank_akshare(code: str, *, ak_module: Any = None) -> int | None:
    """通过 ``stock_hot_rank_em()``（无参 top100）取人气榜排名，作为生产环境兜底。

    沙箱内该函数会抛 ProxyError（push2 域名不可达）；生产需确认 push2 可达。
    返回 ``code`` 的排名（1-100）；未上榜/接口失败 → None。
    """
    ak = ak_module
    if ak is None:
        import akshare

        ak = akshare

    sec_code = normalize_code(code)
    if not sec_code:
        return None
    try:
        df = ak.stock_hot_rank_em()
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    code_col = "代码" if "代码" in df.columns else "股票代码"
    rank_col = "当前排名" if "当前排名" in df.columns else "排名"
    if code_col not in df.columns or rank_col not in df.columns:
        return None
    rows = df[df[code_col].astype(str).str.strip() == sec_code]
    if rows.empty:
        return None
    return to_int(rows.iloc[0].get(rank_col))
