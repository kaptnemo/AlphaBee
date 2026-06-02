from pathlib import Path

from alphabee.tools.common import web_search
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from alphabee.utils import create_chat_model
from alphabee.middleware.common import check_message_limit
from alphabee.agents.derived_facts.prompts import DERIVED_FACT_AGENT_PROMPT
from alphabee.agents.derived_facts.tools import evaluate_derived_facts

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_SOURCES = Path(__file__).parent / "skills"

def derived_fact_agent_factory():
    """衍生事实代理工厂：创建并返回一个 DerivedFactAgent 实例。"""
    
    backend = FilesystemBackend(root_dir=str(_PROJECT_ROOT), virtual_mode=True)
    skills = [str(_SKILLS_SOURCES)] if _SKILLS_SOURCES.exists() else None
    return create_deep_agent(
        model=create_chat_model("agent.derived_facts"),
        system_prompt=DERIVED_FACT_AGENT_PROMPT,
        tools=[
            evaluate_derived_facts,
        ],
        middleware=[
            check_message_limit,
        ],
        backend=backend,
        skills=skills,
    )
