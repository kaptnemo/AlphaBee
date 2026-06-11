from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from alphabee.utils import create_chat_model
from alphabee.middleware.common import check_message_limit
from alphabee.agents.signal.prompts import SIGNAL_AGENT_PROMPT
from alphabee.agents.signal.tools import evaluate_signals, list_signal_rules

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def signal_agent_factory():
    """信号代理工厂：创建并返回一个 SignalAgent 实例。"""
    backend = FilesystemBackend(root_dir=str(_PROJECT_ROOT), virtual_mode=True)
    return create_deep_agent(
        model=create_chat_model("agent.signal"),
        system_prompt=SIGNAL_AGENT_PROMPT,
        tools=[
            list_signal_rules,
            evaluate_signals,
        ],
        middleware=[
            check_message_limit,
        ],
        backend=backend,
    )