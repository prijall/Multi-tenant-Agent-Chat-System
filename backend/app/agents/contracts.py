from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    role: str
    parts: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ProviderTurn:
    parts: list[dict[str, Any]]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class StreamingProvider(Protocol):
    def stream(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[str | ProviderTurn]:
        """Yield text as it arrives, then the finished turn last."""


class AgentProvider(Protocol):
    async def complete(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn: ...
