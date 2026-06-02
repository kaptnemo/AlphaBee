from typing import TypeVar
from deepagents import create_deep_agent
from pydantic import BaseModel

from alphabee.agents_legacy.industry.prompts import INDUSTRY_AGENT_PROMPT
from alphabee.tools.industry_fundamentals import get_industry_fundamentals
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction

T = TypeVar("T", default=str)


def industry_agent_factory(resultType: T, example: str = "") -> T:
    """行业分析代理，专注于分析行业/产业的景气度、估值水平、竞争格局、政策环境和发展趋势等因素，提供清晰、简洁、基于数据的分析结果。"""
    if issubclass(resultType, BaseModel):
        return create_deep_agent(
            model=create_chat_model("agent.industry"),
            system_prompt=INDUSTRY_AGENT_PROMPT + "\n\n" + json_instruction(
                schema=resultType,
                example="" + example if example else "无"
            ),
            tools=[
                get_industry_fundamentals,
                # web_search,
            ],
            middleware=[
                check_message_limit,
            ]
        )
    elif issubclass(resultType, str):
        return create_deep_agent(
            model=create_chat_model("agent.industry"),
            system_prompt=INDUSTRY_AGENT_PROMPT + "\n\n" + "请直接返回简洁的分析结论，使用markdown格式，不要代码块，不要额外解释。",
            tools=[
                get_industry_fundamentals,
                # web_search,
            ],
            middleware=[
                check_message_limit,
            ]
        )
    else:
        raise ValueError("Unsupported result type for industry agent")
