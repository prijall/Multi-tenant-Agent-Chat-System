import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from app.agents.contracts import AgentEvent, AgentProvider, ProviderTurn
from app.agents.gemini import GeminiProvider
from app.agents.mistral import MistralProvider
from app.core.config import Settings

logger = logging.getLogger(__name__)

PROVIDERS = {
    "gemini": (GeminiProvider, "gemini-3.5-flash-lite"),
    "mistral": (MistralProvider, "ministral-14b-latest"),
}


class FallbackProvider:
    """Uses the primary, and the fallback if the primary errors or is silent for too long.

    The switch only happens before any of the reply has reached the customer, so nobody sees
    half an answer from one model followed by a whole answer from another. One instance serves
    one turn: once the primary has failed, the rest of that turn stays on the fallback, and the
    next message tries the primary again.
    """

    def __init__(
        self, primary: AgentProvider, fallback: AgentProvider, wait_seconds: float = 10
    ):
        self.primary = primary
        self.fallback = fallback
        self.wait_seconds = wait_seconds
        self.primary_failed = False

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
        arguments = {"system_prompt": system_prompt, "events": events, "tools": tools}
        if not self.primary_failed:
            async with aclosing(self.primary.stream(**arguments)) as items:
                try:
                    async with asyncio.timeout(self.wait_seconds):
                        first = await anext(items)
                except (Exception, StopAsyncIteration) as exc:
                    self.primary_failed = True
                    logger.warning(
                        "%s failed or took over %ss (%s); using %s",
                        type(self.primary).__name__,
                        self.wait_seconds,
                        type(exc).__name__,
                        type(self.fallback).__name__,
                    )
                else:
                    yield first
                    async for item in items:
                        yield item
                    return
        async with aclosing(self.fallback.stream(**arguments)) as items:
            async for item in items:
                yield item


def _single_provider(name: str | None, api_key: str | None, model: str | None):
    choice = PROVIDERS.get((name or "").strip().lower())
    if choice is None or not api_key:
        return None
    provider_class, default_model = choice
    return provider_class(api_key, model or default_model)


def build_primary_provider(settings: Settings) -> AgentProvider | None:
    return _single_provider(settings.llm_provider, settings.llm_api_key, settings.llm_model)


def _fallback(settings: Settings) -> AgentProvider | None:
    return _single_provider(
        settings.llm_fallback_provider,
        settings.llm_fallback_api_key,
        settings.llm_fallback_model,
    )


def build_suggestion_provider(settings: Settings) -> AgentProvider | None:
    """Suggestions run on the fallback when there is one, keeping the main quota for chat."""
    return _fallback(settings) or build_primary_provider(settings)


def build_provider(settings: Settings) -> AgentProvider | None:
    """The LLM is chosen by environment variables alone; None means it isn't configured."""
    primary = build_primary_provider(settings)
    fallback = _fallback(settings)
    if primary is None or fallback is None:
        return primary or fallback
    return FallbackProvider(primary, fallback, settings.llm_fallback_after_seconds)
