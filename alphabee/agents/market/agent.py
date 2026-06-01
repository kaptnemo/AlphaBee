from pathlib import Path
from typing import TypeVar

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from pydantic import BaseModel

from alphabee.agents.market.prompts import MARKET_AGENT_PROMPT
from alphabee.tools.tushare_query import query_tushare
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction

T = TypeVar("T", default=str)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_SOURCES = [str(_PROJECT_ROOT / ".github" / "skills")]


def market_agent_factory(resultType: T, example: str = "") -> T:
    """市场行情分析代理，通过 Tushare skill 自主决定获取哪些行情数据，分析价格、成交量、资金流向和板块热度等。"""
    backend = FilesystemBackend(root_dir=str(_PROJECT_ROOT), virtual_mode=True)
    if issubclass(resultType, BaseModel):
        return create_deep_agent(
            model=create_chat_model("agent.market"),
            system_prompt=MARKET_AGENT_PROMPT + "\n\n" + json_instruction(
                schema=resultType,
                example="" + example if example else "无"
            ),
            tools=[
                query_tushare,
            ],
            middleware=[
                check_message_limit,
            ],
            backend=backend,
            skills=_SKILLS_SOURCES,
        )
    elif issubclass(resultType, str):
        return create_deep_agent(
            model=create_chat_model("agent.market"),
            system_prompt=MARKET_AGENT_PROMPT + "\n\n" + "请直接返回简洁的分析结论，使用markdown格式，不要代码块，不要额外解释。",
            tools=[
                query_tushare,
            ],
            middleware=[
                check_message_limit,
            ],
            backend=backend,
            skills=_SKILLS_SOURCES,
        )
    else:
        raise ValueError("Unsupported result type for market agent")
