from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from alphabee.agents.industry.prompts import INDUSTRY_AGENT_PROMPT
from alphabee.config import settings
from alphabee.tools.industry_fundamentals import get_industry_fundamentals
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit


industry_agent = create_deep_agent(
    model=ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    ),
    system_prompt=INDUSTRY_AGENT_PROMPT,
    tools=[
        get_industry_fundamentals,
        # web_search,
    ],
    middleware=[
        check_message_limit,
    ]
)
