from typing import TypeVar
from deepagents import create_deep_agent
from pydantic import BaseModel

from alphabee.agents.market.prompts import MARKET_AGENT_PROMPT
from alphabee.tools.market_data import get_market_data
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction

T = TypeVar("T", default=str)


def market_agent_factory(resultType: T, example: str = "") -> T:
    """市场分析代理，专注于分析价格、成交量、技术趋势、板块热度和资金流向等市场数据，提供简洁、客观、基于数据的分析结果。"""
    if issubclass(resultType, BaseModel):
        return create_deep_agent(
            model=create_chat_model("agent.market"),
            system_prompt=MARKET_AGENT_PROMPT + "\n\n" + json_instruction(
                schema=resultType,
                example="" + example if example else "无"
            ),
            tools=[
                get_market_data,
            ],
            middleware=[
                check_message_limit,
            ]
        )
    elif issubclass(resultType, str):
        return create_deep_agent(
            model=create_chat_model("agent.market"),
            system_prompt=MARKET_AGENT_PROMPT + "\n\n" + "请直接返回简洁的分析结论，使用markdown格式，不要代码块，不要额外解释。",
            tools=[
                get_market_data,
            ],
            middleware=[
                check_message_limit,
            ]
        )
    else:
        raise ValueError("Unsupported result type for market agent")
