"""`web_search_guard` 的 R-1（词边界匹配）与 R-2（驱动变量窄白名单）用例。

覆盖 docs/design/DRIVER_PROFILE_D0_CODING_DESIGN.md §2.3 与 §3.4：
- R-1：`pe|pb|ps` 词边界化后 `capex` / `pipeline` 不再误拦，中文禁词行为逐字不变；
- R-2：行业/商品驱动变量的纯定性查询放行，财务禁词查询仍短路；
- 放行路径上 post-call 免责声明与数值扫描照旧执行。
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from alphabee.middleware.web_search_guard import (
    _detect_forbidden,
    _is_driver_variable_query,
    web_search_guard,
)

_SEARCH_CONTENT = "机构调研显示行业景气度回升，公司最新价 15.20 元。"


class _Handler:
    """记录 handler 是否被调用，用于区分「pre-call 短路」与「放行」。"""

    def __init__(self, content: str = _SEARCH_CONTENT) -> None:
        self.content = content
        self.calls: list[str] = []

    async def __call__(self, request: ToolCallRequest) -> ToolMessage:
        self.calls.append(request.tool_call["args"]["query"])
        return ToolMessage(content=self.content, tool_call_id=request.tool_call["id"])


def _request(query: str, tool_name: str = "web_search") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {"query": query}, "id": "call-1"},
        tool=None,
        state=None,
        runtime=None,
    )


async def _run(query: str, handler: _Handler, tool_name: str = "web_search") -> ToolMessage:
    result = await web_search_guard.awrap_tool_call(_request(query, tool_name), handler)
    assert isinstance(result, ToolMessage)
    return result


# ---------------------------------------------------------------------------
# §2.3 R-1：词边界匹配
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["capex 周期", "pipeline 进展"])
def test_pe_substring_is_not_triggered(query: str) -> None:
    """`capex` / `pipeline` 内含 `pe` 子串，词边界化后不得触发禁词。"""
    assert _detect_forbidden(query) == (False, "")


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("AI PE 估值", "市值/估值指标"),
        ("行业 PE 估值", "行业行情数据"),
        ("PB 估值", "市值/估值指标"),
        ("PS 估值", "市值/估值指标"),
        # 中文侧禁词行为逐字不变
        ("股价", "股票价格/涨跌"),
        ("市盈率", "市值/估值指标"),
        ("营收", "财务数字"),
    ],
)
def test_forbidden_labels(query: str, reason: str) -> None:
    assert _detect_forbidden(query) == (True, reason)


# ---------------------------------------------------------------------------
# §3.4 R-2：驱动变量窄白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["生猪 2026 下半年 猪价 走势", "铜价 能繁母猪 存栏", "碳酸锂 产能利用率 排产"],
)
def test_is_driver_variable_query_allowed(query: str) -> None:
    assert _is_driver_variable_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "宁德时代 净利润",  # 财务禁词命中，白名单不救
        "capex 周期",  # 白名单与禁词均不命中（R-1 已不拦）
        "猪价 涨跌幅",  # 命中白名单但同时命中财务/股价禁词 → 不放行
        "行业 PE 估值",  # 命中禁词（行业行情数据）→ 不放行
    ],
)
def test_is_driver_variable_query_rejected(query: str) -> None:
    assert _is_driver_variable_query(query) is False


@pytest.mark.parametrize("query", ["生猪 2026 下半年 猪价 走势", "铜价 能繁母猪 存栏", "capex 周期"])
async def test_driver_variable_query_passes_pre_call(query: str) -> None:
    """驱动变量查询不得被 pre-call 短路，真实调用照常发起。"""
    handler = _Handler()
    result = await _run(query, handler)

    assert handler.calls == [query]
    assert "[web_search 已被拦截]" not in result.content
    assert "web_search 数据免责声明" in result.content


async def test_financial_query_still_blocked() -> None:
    """财务禁词查询仍短路，不发起真实调用。"""
    handler = _Handler()
    result = await _run("宁德时代 净利润", handler)

    assert handler.calls == []
    assert result.content.startswith("[web_search 已被拦截]")
    assert "财务数字" in result.content


async def test_post_call_disclaimer_and_numeric_scan_unchanged() -> None:
    """放行路径上 post-call 免责声明与数值核验指令注入照旧执行。"""
    handler = _Handler("公司最新价 15.20 元，另有机构给出总市值 3200 亿。")
    result = await _run("生猪 猪价 走势", handler)

    assert "web_search 数据免责声明" in result.content
    assert "数值核验指令" in result.content
    assert "get_market_data" in result.content


async def test_non_web_search_tool_untouched() -> None:
    """非 web_search 工具调用不被守卫改写。"""
    handler = _Handler()
    result = await _run("股价", handler, tool_name="get_market_data")

    assert handler.calls == ["股价"]
    assert result.content == _SEARCH_CONTENT
