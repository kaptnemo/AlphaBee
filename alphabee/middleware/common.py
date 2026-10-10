from typing import Any

from langchain.agents.middleware import AgentState, before_model
from langchain.messages import AIMessage
from langgraph.runtime import Runtime

# 研究类 agent 的消息上限默认值：≥ 该值直接终止会话（非截断）
DEFAULT_MESSAGE_LIMIT = 50


def _message_limit() -> int:
    """读取研究类 agent 的消息上限。

    局部 import + ``getattr`` 兜底：避免 middleware 在 import 期拉起配置加载副作用，
    也保证缺失该配置项时行为回落到 ``DEFAULT_MESSAGE_LIMIT``（默认 50）。
    """
    from alphabee.config import get_settings

    return getattr(get_settings(), "message_limit", DEFAULT_MESSAGE_LIMIT)


@before_model(can_jump_to=["end"])
def check_message_limit(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    if len(state["messages"]) >= _message_limit():
        return {"messages": [AIMessage("Conversation limit reached.")], "jump_to": "end"}
    return None
