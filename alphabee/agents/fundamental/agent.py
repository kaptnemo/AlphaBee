from typing import TypeVar
from deepagents import create_deep_agent
from pydantic import BaseModel

from alphabee.agents.fundamental.prompts import FUNDAMENTAL_AGENT_PROMPT
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction

T = TypeVar("T", default=str)


def fundamental_agent_factory(resultType: T, example: str = "") -> T:
    """基本面分析代理，专注于分析公司的盈利能力、成长性、财务健康、行业地位和护城河等基本面因素，提供清晰、简洁、基于数据的分析结果。"""
    if issubclass(resultType, BaseModel):
        return create_deep_agent(
            model=create_chat_model("agent.fundamental"),
            system_prompt=FUNDAMENTAL_AGENT_PROMPT + "\n\n" + json_instruction(
                schema=resultType,
                example="" + example if example else "无"
            ),
            tools=[
                web_search,
                get_fundamentals,
            ],
            middleware=[
                web_search_guard,
                check_message_limit,
            ]
        )
    elif issubclass(resultType, str):
        return create_deep_agent(
            model=create_chat_model("agent.fundamental"),
            system_prompt=FUNDAMENTAL_AGENT_PROMPT + "\n\n" + "请直接返回简洁的分析结论，使用markdown格式，不要代码块，不要额外解释。",
            tools=[
                web_search,
                get_fundamentals,
            ],
            middleware=[
                web_search_guard,
                check_message_limit,
            ]
        )
    else:
        raise ValueError("Unsupported result type for fundamental agent")
