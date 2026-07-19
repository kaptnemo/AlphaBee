"""InsightAgent factory — creates an LLM agent for investment viewpoint synthesis."""

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from alphabee.agents.insights.prompts import INSIGHT_AGENT_SYSTEM_PROMPT
from alphabee.middleware.common import check_message_limit
from alphabee.utils import create_chat_model
from alphabee.utils.paths import PROJECT_ROOT


def insight_agent_factory():
    """Create an InsightAgent instance for synthesizing investment viewpoints.

    The InsightAgent is a pure synthesis engine — it works entirely from the
    structured context provided in the user prompt and does not call external
    tools. Its output is an ``InsightOutput`` JSON object consumed by
    downstream thesis and report nodes.
    """
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    return create_deep_agent(
        model=create_chat_model("agent.insights"),
        system_prompt=INSIGHT_AGENT_SYSTEM_PROMPT,
        tools=[],
        middleware=[check_message_limit],
        backend=backend,
    )
