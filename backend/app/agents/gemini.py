import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.transport import (
    MAX_ATTEMPTS,
    MAX_RETRY_WAIT_SECONDS,
    RETRYABLE_STATUS,
    stream_events,
)


def _requested_wait(response: httpx.Response) -> float | None:
    """Read the wait from the RetryInfo detail, such as {"retryDelay": "11s"}."""
    try:
        details = response.json()["error"]["details"]
        delay = next(item["retryDelay"] for item in details if "retryDelay" in item)
        return min(float(delay.rstrip("s")), MAX_RETRY_WAIT_SECONDS)
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return None


def _turn_from_parts(parts: list[dict[str, Any]]) -> ProviderTurn:
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        call = part.get("functionCall")
        if call:
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call-{index}"),
                    name=str(call["name"]),
                    arguments=dict(call.get("args") or {}),
                )
            )
    return ProviderTurn(parts=parts, text=text, tool_calls=calls)


class GeminiProvider:
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

    async def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                wait = self.retry_delay_seconds * attempt
                try:
                    response = await client.post(
                        url,
                        headers={"x-goog-api-key": self.api_key},
                        json=payload,
                    )
                except httpx.TimeoutException:
                    if attempt == MAX_ATTEMPTS:
                        raise
                else:
                    if response.status_code not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                        return response
                    requested = _requested_wait(response)
                    if requested is not None:
                        wait = requested
                await asyncio.sleep(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _url(self, method: str) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:{method}"

    @staticmethod
    def _payload(
        system_prompt: str, events: list[AgentEvent], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": event.role, "parts": event.parts} for event in events],
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": tools}]
            payload["tool_config"] = {"function_calling_config": {"mode": "auto"}}
        return payload

    async def complete(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        response = await self._post(
            self._url("generateContent"), self._payload(system_prompt, events, tools)
        )
        response.raise_for_status()
        candidates = response.json().get("candidates") or []
        if not candidates:
            raise RuntimeError("The model returned no response candidate")
        return _turn_from_parts(candidates[0].get("content", {}).get("parts", []))

    async def stream(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[str | ProviderTurn]:
        parts: list[dict[str, Any]] = []
        async for chunk in stream_events(
            self._url("streamGenerateContent") + "?alt=sse",
            headers={"x-goog-api-key": self.api_key},
            payload=self._payload(system_prompt, events, tools),
            timeout_seconds=self.timeout_seconds,
            retry_delay_seconds=self.retry_delay_seconds,
            requested_wait=_requested_wait,
            transport=self.transport,
        ):
            for candidate in (chunk.get("candidates") or [])[:1]:
                for part in candidate.get("content", {}).get("parts", []):
                    parts.append(part)
                    if part.get("text"):
                        yield str(part["text"])
        if not parts:
            raise RuntimeError("The model returned no response candidate")
        yield _turn_from_parts(parts)
