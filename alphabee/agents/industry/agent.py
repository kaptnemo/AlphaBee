from deepagents import create_deep_agent

from alphabee.agents.industry.prompts import INDUSTRY_AGENT_PROMPT
from alphabee.tools.industry_fundamentals import get_industry_fundamentals
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model


industry_agent = create_deep_agent(
    model=create_chat_model("agent.industry"),
    system_prompt=INDUSTRY_AGENT_PROMPT,
    tools=[
        get_industry_fundamentals,
        # web_search,
    ],
    middleware=[
        check_message_limit,
    ]
)
