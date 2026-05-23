from deepagents import create_deep_agent

from alphabee.agents.market.prompts import MARKET_AGENT_PROMPT
from alphabee.tools.market_data import get_market_data
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model


market_agent = create_deep_agent(
    model=create_chat_model("agent.market"),
    system_prompt=MARKET_AGENT_PROMPT,
    tools=[
        # web_search,
        get_market_data,
    ],
    middleware=[
        check_message_limit,
    ]
)