"""驱动画像研究代理工厂（Tier B，DRIVER_PROFILE_RESEARCH_DESIGN §7.3）。

形态照抄仓库里唯一形态完整的取证代理 ``agents/verify_hypotheses/agent.py``，
但有三处必须遵守的差异（均为设计稿已核实的坑）：

1. ``create_deep_agent`` 的 ``tools=`` 是**增量**的（内建工具含 ``execute``(shell) 与
   文件系统读写），不排除就等于把 shell 交给 agent ⇒ 必须挂 ``_ToolExclusionMiddleware``；
2. ``get_industry_fundamentals`` 必须**真正挂进 tools=**：它是 ``web_search_guard``
   拦截文案里推荐的替代工具，此前未注册到任何 agent，拦截文案等于在误导模型；
3. **不设 response_format**：``api.deepseek.com`` 端点不支持 json_schema，
   ``create_deep_agent`` 的 response_format 会走 ProviderStrategy/ToolStrategy 而失败
   （``utils/llm.py:235-248`` 已定论）⇒ 走 ``json_instruction`` + ``parse_json`` + Tier C。
"""

from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware
from langchain.agents.middleware import ToolRetryMiddleware
from langgraph.graph.state import CompiledStateGraph

from alphabee import PROJECT_ROOT
from alphabee.agents.driver_profile.output import DriverResearchOutput
from alphabee.agents.driver_profile.prompts import DRIVER_PROFILE_RESEARCH_PROMPT
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.common import web_search
from alphabee.tools.industry_fundamentals import get_industry_fundamentals
from alphabee.tools.tushare_query import query_tushare
from alphabee.utils import create_chat_model, json_instruction

#: 硬性禁用的内建工具（shell + 文件系统）：研究代理只通过工具取证，不得自行浏览本地目录
#: （``reports/`` 等一手材料由 ``query_financial_report`` 以受控方式提供）。
EXCLUDED_TOOLS = frozenset({"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"})


def driver_profile_research_agent_factory() -> CompiledStateGraph[Any, Any, Any, Any]:
    """创建驱动画像研究代理（Tier B）。

    ``query_financial_report`` 延迟导入：``alphabee.tools.financial_report`` 会拉起
    deepagents/fetch 全家桶，导入成本较高，只在真正构建研究代理时才加载。
    """
    from alphabee.tools.financial_report import query_financial_report

    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    system_prompt = DRIVER_PROFILE_RESEARCH_PROMPT + "\n\n" + json_instruction(DriverResearchOutput)

    return create_deep_agent(
        model=create_chat_model("agent.driver_profile"),
        system_prompt=system_prompt,
        tools=[
            query_financial_report,
            query_tushare,
            web_search,
            get_industry_fundamentals,
        ],
        middleware=[
            # 研究类 agent 的消息上限（≥ 阈值直接终止，非截断）：阈值可配置，
            # 保证 2 × max_tool_calls + 余量 < 阈值，避免工具预算被静默接管。
            check_message_limit,
            web_search_guard,
            ToolRetryMiddleware(),
            _ToolExclusionMiddleware(excluded=EXCLUDED_TOOLS),
        ],
        backend=backend,
        skills=[
            "alphabee/skills/tushare",
            "alphabee/skills/eastmoney",
        ],
        # 不设 response_format=（端点不支持 json_schema，见模块 docstring）。
    )


if __name__ == "__main__":
    import asyncio

    agent = driver_profile_research_agent_factory()
    print("DriverProfileResearchAgent created successfully.")
    asyncio.run(
        agent.ainvoke({"messages": [{"role": "user", "content": "请研究一下宁德时代（300750.SZ）的利润驱动变量。"}]})
    )
