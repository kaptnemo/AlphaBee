from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field


class AgentResult(BaseModel):
    """AgentResult is the result of an agent's execution."""

    agent_name: str = Field(..., description="The name of the agent.")
    success: bool = Field(..., description="Whether the agent's execution was successful.")
    summary: str = Field(..., description="A summary of the agent's execution.")
    data: dict[str, Any] = Field(default_factory=dict, description="Additional data related to the agent's execution.")
    confidence: float = Field(..., description="The confidence score of the agent's execution.")
    error: str | None = Field(default=None, description="An error message if the agent's execution failed.")
    elapsed_ms: int = Field(default=0, description="The time taken for the agent's execution in milliseconds.")


class AgentContext(BaseModel):
    """AgentContext is the context in which an agent operates."""

    user_id: str = Field(..., description="The ID of the user for whom the agent is operating.")
    trace_id: str = Field(..., description="The trace ID for tracking the agent's execution.")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata related to the agent's execution."
    )


ToolFn = Callable[..., Any]


class AgentBase(ABC):
    """AgentBase is the base class for all agents."""

    def __init__(
        self,
        name: str,
        description: str,
        tools: dict[str, ToolFn] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.tools = tools or {}

    async def execute(
        self,
        state: BaseModel | dict[str, Any],
        context: AgentContext | None = None,
    ) -> AgentResult:
        """Execute the agent with the given context.

        Args:
            context (AgentContext): The context in which the agent operates.

        Returns:
            AgentResult: The result of the agent's execution.
        """
        try:
            result = await self.run(state=state, context=context)

            if not isinstance(result, AgentResult):
                raise ValueError("The run method must return an instance of AgentResult.")

            return result
        except Exception as e:
            return AgentResult(
                agent_name=self.name,
                success=False,
                summary=str(e),
                confidence=0.0,
                error=str(e),
            )

    @abstractmethod
    async def run(
        self,
        state: BaseModel | dict[str, Any],
        context: AgentContext | None = None,
    ) -> AgentResult:
        """Run the agent with the given context.

        Args:
            state (BaseModel | dict[str, Any]): The state in which the agent operates.
            context (AgentContext | None): The context in which the agent operates.

        Returns:
            AgentResult: The result of the agent's execution.
        """
        raise NotImplementedError("The run method must be implemented by subclasses of AgentBase.")

    def register_tool(self, name: str, tool_fn: ToolFn) -> None:
        self.tools[name] = tool_fn

    def get_tool(self, name: str) -> ToolFn:
        if name not in self.tools:
            raise ValueError(f"Tool '{name}' is not registered.")
        return self.tools.get(name)

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        tool_fn = self.get_tool(name)

        result = tool_fn(**kwargs)

        if hasattr(result, "__await__"):
            result = await result

        return result

    def read_state(
        self,
        state: BaseModel | dict[str, Any],
        key: str,
        default: Any = None,
    ) -> Any:
        if isinstance(state, BaseModel):
            return getattr(state, key, default)
        elif isinstance(state, dict):
            return state.get(key, default)
        else:
            raise ValueError("State must be a BaseModel or a dict.")

    def success(self, summary: str, data: dict[str, Any] | None = None, confidence: float = 1.0) -> AgentResult:
        return AgentResult(
            agent_name=self.name,
            success=True,
            summary=summary,
            data=data or {},
            confidence=confidence,
        )

    def failure(self, summary: str, error: str | None = None) -> AgentResult:
        return AgentResult(
            agent_name=self.name,
            success=False,
            summary=summary,
            data={},
            confidence=0.0,
            error=error,
        )
