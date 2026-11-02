"""Server-sent events from an LLM API, with the retries free tiers need."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

# Free-tier limits and demand spikes clear quickly, so these are worth trying again.
RETRYABLE_STATUS = {429, 500, 503}
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 30


async def stream_events(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    retry_delay_seconds: float,
    requested_wait: Callable[[httpx.Response], float | None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Retries happen only before the first event, so a reply is never shown twice."""
    async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = retry_delay_seconds * attempt
            started = False
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code < 400:
                        async for line in response.aiter_lines():
                            data = line[len("data:") :].strip() if line.startswith("data:") else ""
                            if data and data != "[DONE]":
                                started = True
                                yield json.loads(data)
                        return
                    await response.aread()
                    if response.status_code not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                        response.raise_for_status()
                    requested = requested_wait(response)
                    if requested is not None:
                        wait = min(requested, MAX_RETRY_WAIT_SECONDS)
            except httpx.TimeoutException:
                if started or attempt == MAX_ATTEMPTS:
                    raise
            await asyncio.sleep(wait)
