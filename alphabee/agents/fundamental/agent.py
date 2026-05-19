from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from alphabee.agents.fundamental.prompts import FUNDAMENTAL_AGENT_PROMPT
from alphabee.config import settings
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.common import web_search

fundamental_agent = create_deep_agent(
    model=ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    ),
    system_prompt=FUNDAMENTAL_AGENT_PROMPT,
    tools=[
        # web_search,
        get_fundamentals,
    ]
)
