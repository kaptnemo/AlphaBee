from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from alphabee.utils.paths import PROJECT_ROOT
from alphabee.utils import create_chat_model, json_instruction
from alphabee.middleware.common import check_message_limit
from alphabee.agents.explore_conflicts.prompts import EXPLORE_CONFLICTS_PROMPT
from alphabee.agents.schemas import ConflictAnalysisResult


def explore_conflicts_agent_factory():
    """冲突探索代理工厂：创建并返回一个 ExploreConflictsAgent 实例。"""
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)

    system_prompt = (
        EXPLORE_CONFLICTS_PROMPT
        + "\n\n"
        + json_instruction(ConflictAnalysisResult)
    )
    return create_deep_agent(
        model=create_chat_model("agent.explore_conflicts"),
        system_prompt=system_prompt,
        middleware=[
            check_message_limit,
        ],
        backend=backend,
    )
