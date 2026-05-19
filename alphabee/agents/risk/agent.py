
from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from alphabee.agents.risk.prompts import RISK_AGENT_PROMPT
from alphabee.config import settings
from alphabee.tools.market_data import get_market_data
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.news import get_stock_news_summary
from alphabee.tools.common import web_search


risk_agent = create_deep_agent(
    model=ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    ),
    system_prompt=RISK_AGENT_PROMPT,
    tools=[
        web_search,
        get_market_data,
        get_fundamentals,
        get_stock_news_summary,
    ]
)