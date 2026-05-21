"""Common tools — general-purpose utilities available to all agents."""

import re
import asyncio
from typing import Literal

from pathlib import Path

import structlog
from pydantic import BaseModel, Field
from opencc import OpenCC

from alphabee.config import settings

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    """单条搜索结果"""
    title: str = Field(description="结果标题")
    url: str = Field(description="来源 URL")
    snippet: str = Field(description="摘要内容")
    score: float = Field(default=0.0, description="相关性评分（Tavily 提供，0-1）")


class WebSearchResponse(BaseModel):
    """网络搜索结果汇总"""
    query: str = Field(description="搜索查询词")
    answer: str = Field(default="", description="AI 生成的综合摘要（仅 Tavily 提供）")
    results: list[SearchResult] = Field(description="搜索结果列表")
    source: Literal["tavily", "ddgs"] = Field(description="数据来源")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_response(resp: WebSearchResponse) -> str:
    """Convert WebSearchResponse to a concise LLM-readable string."""
    lines: list[str] = []
    if resp.answer:
        lines.append(f"【综合摘要】\n{resp.answer}\n")
    lines.append(f"【搜索结果】（来源: {resp.source}，共 {len(resp.results)} 条）")
    for i, r in enumerate(resp.results, 1):
        lines.append(f"\n[{i}] {r.title}")
        lines.append(f"    {r.url}")
        lines.append(f"    {r.snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tavily backend
# ---------------------------------------------------------------------------

async def _search_tavily(
    query: str,
    topic: str,
    max_results: int,
    days: int | None,
) -> WebSearchResponse:
    cfg = settings.web_search.tavily
    if not cfg.api_key:
        raise EnvironmentError("Tavily API key not configured (TAVILY_API_KEY or config.yaml)")

    from tavily import AsyncTavilyClient

    client_kwargs: dict = {"api_key": cfg.api_key}
    if cfg.proxy_url:
        client_kwargs["proxies"] = {
            "http://": cfg.proxy_url,
            "https://": cfg.proxy_url,
        }
    client = AsyncTavilyClient(**client_kwargs)

    search_kwargs: dict = dict(
        query=query,
        search_depth="basic",
        topic=topic,
        max_results=max_results,
        include_answer=True,
        timeout=cfg.timeout,
    )
    if days is not None:
        search_kwargs["days"] = days

    cc = OpenCC('t2s')

    raw: dict = await client.search(**search_kwargs)

    results = [
        SearchResult(
            title=cc.convert(r.get("title", "")),
            url=r.get("url", ""),
            snippet=cc.convert(r.get("content", "")),
            score=float(r.get("score", 0.0)),
        )
        for r in raw.get("results", [])
    ]
    return WebSearchResponse(
        query=query,
        answer=raw.get("answer", ""),
        results=results,
        source="tavily",
    )


# ---------------------------------------------------------------------------
# DDGS fallback backend (ddgs package, sync → asyncio.to_thread)
# ---------------------------------------------------------------------------

def _search_ddg_sync(
    query: str,
    max_results: int,
    timelimit: str | None,
) -> WebSearchResponse:
    from ddgs import DDGS

    cfg = settings.web_search.ddgs

    def _run(tl: str | None) -> list[dict]:
        with DDGS(proxy=cfg.proxy_url, timeout=cfg.timeout) as ddgs:
            return ddgs.text(
                query,
                region=cfg.region,
                safesearch="off",
                timelimit=tl,
                max_results=max_results,
            ) or []

    raw: list[dict] = []
    try:
        raw = _run(timelimit)
    except Exception:
        # timelimit filter can fail for some locales; retry without it
        if timelimit is not None:
            try:
                raw = _run(None)
            except Exception:
                pass

    return WebSearchResponse(
        query=query,
        answer="",
        results=[
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
            )
            for r in raw
        ],
        source="ddgs",
    )


async def _search_ddgs(
    query: str,
    max_results: int,
    days: int | None,
) -> WebSearchResponse:
    timelimit: str | None = None
    if days is not None:
        if days <= 1:
            timelimit = "d"
        elif days <= 7:
            timelimit = "w"
        elif days <= 30:
            timelimit = "m"
        else:
            timelimit = "y"
    return await asyncio.to_thread(_search_ddg_sync, query, max_results, timelimit)


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------

async def web_search(
    query: str,
    topic: Literal["general", "news", "finance"] = "general",
    max_results: int | None = None,
    days: int | None = None,
) -> str:
    """在互联网上搜索实时信息，返回摘要和来源列表。

    当用户询问的内容超出本地数据范围时调用，例如：
    - 最新行业动态、政策消息、市场新闻
    - 公司公告、融资、并购、人事变动等事件
    - 宏观经济数据、行业报告关键数据
    - 任何需要实时性或时效性的信息

    优先使用 Tavily（需在 config.yaml 或 TAVILY_API_KEY 环境变量中配置 API Key）；
    Tavily 不可用时自动回退到 DDGS。

    Args:
        query:       搜索关键词，尽量用具体描述性语句，例如
                     "宁德时代2025年一季度净利润" 而非 "宁德时代业绩"
        topic:       搜索主题类型，可选值：
                     - "general"（默认）：通用搜索
                     - "news"：新闻搜索，优先返回最新资讯
                     - "finance"：金融专题搜索（Tavily 专有，回退时按 general 处理）
        max_results: 返回结果数量，默认使用配置文件中的值（6），最多 20 条
        days:        限制结果时间范围（天），如 7 表示最近 7 天，None 表示不限

    Returns:
        格式化的搜索结果字符串，包含：
        - 【综合摘要】AI 生成的答案（仅 Tavily 提供）
        - 【搜索结果】每条含标题、URL、摘要内容
    """
    n = min(max_results or settings.web_search.tavily.max_results, 20)

    # ── 1. Try Tavily ────────────────────────────────────────────────────────
    try:
        resp = await _search_tavily(query, topic, n, days)
        logger.info("web_search.tavily_ok", query=query, n=len(resp.results))
        return _format_response(resp)
    except EnvironmentError as exc:
        logger.info("web_search.tavily_skip", reason=str(exc))
    except Exception as exc:
        logger.warning("web_search.tavily_failed", error=str(exc), query=query)

    # ── 2. DDGS fallback ───────────────────────────────────────────────
    ddg_n = min(max_results or settings.web_search.ddgs.max_results, 20)
    try:
        resp = await _search_ddgs(query, ddg_n, days)
        if not resp.results:
            logger.warning("web_search.ddg_empty", query=query)
            return (
                f"搜索未返回结果（查询: {query}）。\n"
                "提示：在 config.yaml 中配置 TAVILY_API_KEY 可获得更稳定的搜索结果。"
            )
        logger.info("web_search.ddg_ok", query=query, n=len(resp.results))
        return _format_response(resp)
    except Exception as exc:
        logger.error("web_search.ddg_failed", error=str(exc), query=query)
        return (
            f"搜索失败（查询: {query}）：{exc}\n"
            "提示：在 config.yaml 中配置 TAVILY_API_KEY 可获得更稳定的搜索结果。"
        )


def extract_symbols_from_query(query: str) -> dict[str, str]:
    """从用户查询中提取股票代码或公司名称，返回标准化的股票名称与代码映射。目前仅支持 A 股。

    例如：
    - "宁德时代最新的海外订单情况" → {"宁德时代": "300750.SZ"}
    - "分析一下特斯拉和比亚迪的财报" → {"特斯拉": "TSLA", "比亚迪": "002594.SZ"}

    这个函数可以使用简单的正则表达式、预定义的公司列表，或者调用外部 API 来实现。
    目前实现一个非常基础的版本，仅供示例。
    """
    all_stocks_path = Path(__file__).resolve().parents[1] / "static" / "all_stocks.csv"

    if all_stocks_path.exists():
        import pandas as pd
        df = pd.read_csv(all_stocks_path)
        company_map = dict(zip(df["name"], df["ts_code"]))
    else:
        raise FileNotFoundError(f"Stock list not found at {all_stocks_path}. Please run the tushare collector to generate it.")

    symbols = []
    for name, code in company_map.items():
        if name in query:
            symbols.append(code)

    # 也可以尝试直接匹配股票代码（如 6位数字 + .SZ/.SH）
    code_pattern = re.compile(r"\b\d{6}\.(SZ|SH)\b", re.IGNORECASE)
    matches = code_pattern.findall(query)
    symbols.extend(matches)

    return {name: code for name, code in company_map.items() if code in symbols}


if __name__ == "__main__":
    # import asyncio

    # query = "宁德时代最新的海外订单情况"

    # result = asyncio.run(
    #     web_search(
    #         query=query,
    #         topic="finance"
    #     )
    # )

    # print(result)

    result = extract_symbols_from_query("分析一下特斯拉和比亚迪的财报")
    print(result)
