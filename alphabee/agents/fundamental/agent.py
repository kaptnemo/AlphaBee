from deepagents import create_deep_agent

from alphabee.agents.fundamental.prompts import FUNDAMENTAL_AGENT_PROMPT
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.common import web_search
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model

fundamental_agent = create_deep_agent(
    model=create_chat_model("agent.fundamental"),
    system_prompt=FUNDAMENTAL_AGENT_PROMPT,
    tools=[
        web_search,
        get_fundamentals,
    ],
    middleware=[
        web_search_guard,
        check_message_limit,
    ]
)
