from typing import TypeVar

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from pydantic import BaseModel

from alphabee.agents_legacy.fundamental.prompts import FUNDAMENTAL_AGENT_PROMPT
from alphabee.utils.paths import PROJECT_ROOT
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.tushare_query import query_tushare
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction

T = TypeVar("T", default=str)

_SKILLS_SOURCES = [str(PROJECT_ROOT / ".github" / "skills")]


def fundamental_agent_factory(resultType: T, example: str = "") -> T:
    """基本面分析代理，通过 Tushare skill 自主决定获取哪些财务数据，分析公司盈利能力、成长性、财务健康、行业地位和护城河等。"""
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    if issubclass(resultType, BaseModel):
        return create_deep_agent(
            model=create_chat_model("agent.fundamental"),
            system_prompt=FUNDAMENTAL_AGENT_PROMPT + "\n\n" + json_instruction(
                schema=resultType,
                example="" + example if example else "无"
            ),
            tools=[
                query_tushare,
                web_search,
            ],
            middleware=[
                web_search_guard,
                check_message_limit,
            ],
            backend=backend,
            skills=_SKILLS_SOURCES,
        )
    elif issubclass(resultType, str):
        return create_deep_agent(
            model=create_chat_model("agent.fundamental"),
            system_prompt=FUNDAMENTAL_AGENT_PROMPT + "\n\n" + "请直接返回简洁的分析结论，使用markdown格式，不要代码块，不要额外解释。",
            tools=[
                query_tushare,
                web_search,
            ],
            middleware=[
                web_search_guard,
                check_message_limit,
            ],
            backend=backend,
            skills=_SKILLS_SOURCES,
        )
    else:
        raise ValueError("Unsupported result type for fundamental agent")
