import httpx
import pytest

from app.agents.contracts import AgentEvent
from app.agents.gemini import MAX_ATTEMPTS, GeminiProvider

REPLY = {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}


def provider_answering(*answers) -> tuple[GeminiProvider, list[httpx.Request]]:
    """Each answer is a status code, or an exception to raise for that attempt."""
    remaining = list(answers)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = remaining.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json=REPLY if answer == 200 else {"error": {}})

    provider = GeminiProvider(
        "test-key",
        "test-model",
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handle),
    )
    return provider, requests


async def complete(provider: GeminiProvider):
    return await provider.complete(
        system_prompt="Be brief",
        events=[AgentEvent(role="user", parts=[{"text": "Hi"}])],
        tools=[],
    )


@pytest.mark.parametrize(
    "first_failure", [429, 503, httpx.ReadTimeout("slow")], ids=["rate-limit", "busy", "timeout"]
)
async def test_temporary_failures_are_retried(first_failure):
    provider, requests = provider_answering(first_failure, 200)

    turn = await complete(provider)

    assert turn.text == "Hello"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("retry_delay", "expected_wait"), [("11s", 11.0), ("500s", 30.0)], ids=["asked", "capped"]
)
async def test_rate_limit_waits_as_long_as_gemini_asks(monkeypatch, retry_delay, expected_wait):
    waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("app.agents.gemini.asyncio.sleep", record_wait)
    answers = [
        httpx.Response(429, json={"error": {"details": [{"retryDelay": retry_delay}]}}),
        httpx.Response(200, json=REPLY),
    ]
    provider = GeminiProvider(
        "test-key",
        "test-model",
        transport=httpx.MockTransport(lambda _: answers.pop(0)),
    )

    turn = await complete(provider)

    assert turn.text == "Hello"
    assert waits == [expected_wait]


async def test_it_gives_up_after_the_last_attempt():
    provider, requests = provider_answering(*[503] * MAX_ATTEMPTS)

    with pytest.raises(httpx.HTTPStatusError):
        await complete(provider)

    assert len(requests) == MAX_ATTEMPTS


async def test_a_retired_model_is_not_retried():
    provider, requests = provider_answering(404)

    with pytest.raises(httpx.HTTPStatusError):
        await complete(provider)

    assert len(requests) == 1
