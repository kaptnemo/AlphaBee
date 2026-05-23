
from deepagents import create_deep_agent

from alphabee.agents.risk.prompts import RISK_AGENT_PROMPT
from alphabee.tools.market_data import get_market_data
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.news import get_stock_news_summary
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.utils import create_chat_model


risk_agent = create_deep_agent(
    model=create_chat_model("agent.risk"),
    system_prompt=RISK_AGENT_PROMPT,
    tools=[
        web_search,
        get_market_data,
        get_fundamentals,
        get_stock_news_summary,
    ],
    middleware=[
        web_search_guard,
        check_message_limit,
    ]
)