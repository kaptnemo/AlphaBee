"""同行业对标候选闭集（对标组在线兜底，REPORT_QUALITY_FIX_ROADMAP §11 P2-①）。

在线兜底的候选来源是**申万行业成分股闭集**（由 ``resolve_industry_context`` 写入
``IndustryContextArtifact.peer_universe``）。本模块把成分股代码补上公司名，
供 LLM 在**闭集内**择优（只允许选给定代码，杜绝编造不存在的对标）。

公司名来自静态股票表 ``alphabee/static/all_stocks.csv``（本地、无网络），
缺失代码的名称回退为空串（不阻塞选择）。
"""

from __future__ import annotations

import csv
from functools import lru_cache

from alphabee import PROJECT_ROOT

__all__ = ["build_peer_universe", "resolve_current_code_by_name"]

_ALL_STOCKS_CSV = PROJECT_ROOT / "alphabee" / "static" / "all_stocks.csv"

#: 候选闭集上限（控制 LLM prompt token；真对手通常在行业成分内且靠前）。
_DEFAULT_UNIVERSE_LIMIT = 200


@lru_cache(maxsize=1)
def _code_to_name() -> dict[str, str]:
    """静态股票表 ``stock_code → company_name``（读失败 → 空表，名称回退代码）。"""
    if not _ALL_STOCKS_CSV.is_file():
        return {}
    mapping: dict[str, str] = {}
    with _ALL_STOCKS_CSV.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("stock_code") or "").strip().upper()
            name = str(row.get("company_name") or "").strip()
            if code and name and name.lower() != "nan":
                mapping[code] = name
    return mapping


@lru_cache(maxsize=1)
def _name_to_code() -> dict[str, str]:
    """静态股票表 ``company_name → stock_code``（读失败 → 空表）。"""
    return {name: code for code, name in _code_to_name().items()}


def resolve_current_code_by_name(name: str) -> str | None:
    """按公司名反查**当前**股票代码（修复北交所代码迁移等陈旧代码）。

    例：LLM 给出已迁移的旧码 ``873593.BJ``（鼎智科技），静态股票表已更新为
    ``920593.BJ``——存在性校验失败时可按名回查到现用代码。
    """
    key = str(name or "").strip()
    if not key:
        return None
    return _name_to_code().get(key)


def build_peer_universe(
    codes: list[str],
    *,
    exclude: str | None = None,
    limit: int = _DEFAULT_UNIVERSE_LIMIT,
) -> list[dict[str, str]]:
    """成分股代码列表 → ``[{code, name}]`` 闭集候选（去除标的自身、去重、保序、限量）。"""
    names = _code_to_name()
    self_code = str(exclude or "").strip().upper()
    seen: set[str] = set()
    universe: list[dict[str, str]] = []
    for raw in codes:
        code = str(raw).strip().upper()
        if not code or code == self_code or code in seen:
            continue
        seen.add(code)
        universe.append({"code": code, "name": names.get(code, "")})
        if len(universe) >= limit:
            break
    return universe
