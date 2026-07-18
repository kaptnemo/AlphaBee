from typing import Any

from langchain.agents.middleware import AgentState, before_model
from langchain.messages import AIMessage
from langgraph.runtime import Runtime


@before_model(can_jump_to=["end"])
def check_message_limit(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    if len(state["messages"]) >= 50:
        return {"messages": [AIMessage("Conversation limit reached.")], "jump_to": "end"}
    else:
        print(f"messages length: {len(state['messages'])}")
    return None
