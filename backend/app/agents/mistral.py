import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.transport import stream_events

API_URL = "https://api.mistral.ai/v1/chat/completions"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _messages(system_prompt: str, events: list[AgentEvent]) -> list[dict[str, Any]]:
    """Translate the loop's neutral history into Mistral's chat format."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    # Mistral needs 9-character tool ids, and a turn may have started on another provider, so
    # ids are assigned here: each result answers the oldest call still waiting for one.
    waiting: list[str] = []
    issued = 0
    for event in events:
        text = "".join(str(part["text"]) for part in event.parts if "text" in part)
        calls = [part["functionCall"] for part in event.parts if "functionCall" in part]
        results = [part["functionResponse"] for part in event.parts if "functionResponse" in part]
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": waiting.pop(0),
                    "name": result["name"],
                    "content": json.dumps(result["response"]),
                }
            )
        if results:
            continue
        if event.role == "user":
            messages.append({"role": "user", "content": text})
            continue
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = []
            for call in calls:
                call_id = f"call{issued:05d}"
                issued += 1
                waiting.append(call_id)
                message["tool_calls"].append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("args") or {}),
                        },
                    }
                )
        messages.append(message)
    return messages


def _delta_text(content: Any) -> str:
    """Content is a string, or a list of typed chunks for some models."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(chunk.get("text", "")) for chunk in content if isinstance(chunk, dict))
    return ""


class _ToolCallBuilder:
    """A call can arrive whole or spread over several events, with only the first naming it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def add(self, fragment: dict[str, Any]) -> None:
        function = fragment.get("function") or {}
        arguments = function.get("arguments") or ""
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        call_id = fragment.get("id")
        if call_id and call_id not in {call["id"] for call in self.calls} or not self.calls:
            self.calls.append(
                {"id": call_id or "", "name": function.get("name") or "", "arguments": arguments}
            )
            return
        target = next((call for call in self.calls if call["id"] == call_id), self.calls[-1])
        target["name"] = target["name"] or function.get("name") or ""
        target["arguments"] += arguments

    def finished(self) -> list[ToolCall]:
        return [
            ToolCall(
                id=call["id"] or f"call{index}",
                name=call["name"],
                arguments=json.loads(call["arguments"] or "{}"),
            )
            for index, call in enumerate(self.calls)
        ]


class MistralProvider:
    """Small REST adapter; application code remains responsible for every tool call."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 30,
        retry_delay_seconds: float = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.transport = transport

    async def complete(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        turn: ProviderTurn | None = None
        async for item in self.stream(system_prompt=system_prompt, events=events, tools=tools):
            if isinstance(item, ProviderTurn):
                turn = item
        if turn is None:
            raise RuntimeError("The model returned no response")
        return turn

    async def stream(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[str | ProviderTurn]:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "messages": _messages(system_prompt, events),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
            payload["tool_choice"] = "auto"
        text = ""
        builder = _ToolCallBuilder()
        received = False
        async for chunk in stream_events(
            API_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            retry_delay_seconds=self.retry_delay_seconds,
            requested_wait=_retry_after,
            transport=self.transport,
        ):
            for choice in (chunk.get("choices") or [])[:1]:
                received = True
                delta = choice.get("delta") or {}
                piece = _delta_text(delta.get("content"))
                if piece:
                    text += piece
                    yield piece
                for fragment in delta.get("tool_calls") or []:
                    builder.add(fragment)
        if not received:
            raise RuntimeError("The model returned no response")
        calls = builder.finished()
        parts: list[dict[str, Any]] = [{"text": text}] if text else []
        parts += [
            {"functionCall": {"id": call.id, "name": call.name, "args": call.arguments}}
            for call in calls
        ]
        yield ProviderTurn(parts=parts, text=text.strip(), tool_calls=calls)
