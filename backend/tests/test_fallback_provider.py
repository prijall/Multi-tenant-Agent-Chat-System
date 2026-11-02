import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.gemini import GeminiProvider
from app.agents.mistral import MistralProvider
from app.agents.providers import FallbackProvider, build_provider, build_suggestion_provider
from app.agents.runtime import run_agent
from app.core.security import RequestContext
from app.db.models import Conversation, Organization, Product, ToolRun, User

EVENTS = [AgentEvent(role="user", parts=[{"text": "Hi"}])]
PRIMARY = ProviderTurn(parts=[{"text": "from primary"}], text="from primary")
BACKUP = ProviderTurn(parts=[{"text": "from fallback"}], text="from fallback")


@dataclass
class Fake:
    """Each call plays one script: items to yield, exceptions to raise, or a long silence."""

    calls: list[list] = field(default_factory=list)
    used: int = 0

    async def complete(self, **_):
        raise AssertionError("providers are streamed")

    async def stream(self, *, system_prompt, events, tools):
        self.used += 1
        for item in self.calls.pop(0):
            if item == "silence":
                await asyncio.sleep(60)
            elif isinstance(item, Exception):
                raise item
            else:
                yield item


async def collect(provider) -> list:
    return [
        item
        async for item in provider.stream(system_prompt="Be brief", events=EVENTS, tools=[])
    ]


async def test_the_primary_answers_when_healthy():
    primary, fallback = Fake([["Hi", PRIMARY]]), Fake()

    items = await collect(FallbackProvider(primary, fallback))

    assert items == ["Hi", PRIMARY]
    assert fallback.used == 0


async def test_an_error_switches_to_the_fallback():
    primary, fallback = Fake([[RuntimeError("503")]]), Fake([["Hi", BACKUP]])

    items = await collect(FallbackProvider(primary, fallback))

    assert items == ["Hi", BACKUP]


async def test_silence_past_the_limit_switches_to_the_fallback():
    primary, fallback = Fake([["silence"]]), Fake([[BACKUP]])

    started = asyncio.get_running_loop().time()
    items = await collect(FallbackProvider(primary, fallback, wait_seconds=0.05))

    assert items == [BACKUP]
    assert asyncio.get_running_loop().time() - started < 5


async def test_an_empty_response_switches_to_the_fallback():
    primary, fallback = Fake([[]]), Fake([[BACKUP]])

    assert await collect(FallbackProvider(primary, fallback)) == [BACKUP]


async def test_it_does_not_switch_once_the_customer_has_seen_text():
    primary, fallback = Fake([["Half an answer", RuntimeError("lost")]]), Fake([[BACKUP]])
    seen = []

    with pytest.raises(RuntimeError, match="lost"):
        async for item in FallbackProvider(primary, fallback).stream(
            system_prompt="Be brief", events=EVENTS, tools=[]
        ):
            seen.append(item)

    assert seen == ["Half an answer"]
    assert fallback.used == 0


async def test_a_slow_answer_inside_the_limit_is_kept():
    async def slow_stream(**_):
        await asyncio.sleep(0.02)
        yield PRIMARY

    primary = SimpleNamespace(stream=slow_stream)

    items = await collect(FallbackProvider(primary, Fake(), wait_seconds=5))

    assert items == [PRIMARY]


async def test_the_limit_only_covers_the_wait_for_the_first_piece():
    async def long_reply(**_):
        yield "Start"
        await asyncio.sleep(0.1)
        yield PRIMARY

    primary = SimpleNamespace(stream=long_reply)

    items = await collect(FallbackProvider(primary, Fake(), wait_seconds=0.05))

    assert items == ["Start", PRIMARY]


async def test_a_failed_primary_is_skipped_for_the_rest_of_the_turn():
    primary, fallback = Fake([[RuntimeError("503")]]), Fake([[BACKUP], [BACKUP]])
    provider = FallbackProvider(primary, fallback)

    await collect(provider)
    await collect(provider)

    assert primary.used == 1
    assert fallback.used == 2


async def test_when_both_fail_the_error_reaches_the_loop():
    primary, fallback = Fake([[RuntimeError("503")]]), Fake([[ValueError("also down")]])

    with pytest.raises(ValueError, match="also down"):
        await collect(FallbackProvider(primary, fallback))


def llm_settings(**values) -> SimpleNamespace:
    defaults = {
        "llm_provider": "gemini",
        "llm_model": None,
        "llm_api_key": "primary-key",
        "llm_fallback_provider": "mistral",
        "llm_fallback_model": None,
        "llm_fallback_api_key": "fallback-key",
        "llm_fallback_after_seconds": 10,
    }
    return SimpleNamespace(**{**defaults, **values})


def test_settings_build_gemini_with_a_mistral_fallback():
    provider = build_provider(llm_settings())

    assert isinstance(provider, FallbackProvider)
    assert isinstance(provider.primary, GeminiProvider)
    assert isinstance(provider.fallback, MistralProvider)
    assert provider.wait_seconds == 10


def test_without_a_fallback_key_the_primary_runs_alone():
    assert isinstance(build_provider(llm_settings(llm_fallback_api_key=None)), GeminiProvider)


def test_chat_stays_on_gemini_while_suggestions_use_mistral():
    settings = llm_settings()

    assert isinstance(build_provider(settings).primary, GeminiProvider)
    assert isinstance(build_suggestion_provider(settings), MistralProvider)


def test_suggestions_use_the_main_provider_when_there_is_no_fallback():
    settings = llm_settings(llm_fallback_api_key=None)

    assert isinstance(build_suggestion_provider(settings), GeminiProvider)


async def test_a_turn_can_move_to_mistral_after_a_gemini_tool_call(session):
    """Gemini's tool calls carry no ids, so Mistral has to be able to continue from them anyway."""
    org = Organization(slug="himali-tea", name="Himali")
    session.add(org)
    await session.flush()
    user = User(org_id=org.id, email="buyer@test")
    session.add(user)
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    session.add_all(
        [
            conversation,
            Product(
                org_id=org.id, sku="TEA-1", name="First Flush", category="black",
                price=Decimal("500"), stock=3,
            ),
        ]
    )
    await session.commit()
    gemini_call = {"functionCall": {"name": "search_products", "args": {"query": "First"}}}
    primary = Fake(
        [
            [
                ProviderTurn(
                    parts=[gemini_call],
                    tool_calls=[ToolCall("call-0", "search_products", {"query": "First"})],
                )
            ],
            [RuntimeError("503")],
        ]
    )
    sent: list[dict] = []

    def mistral_api(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        chunk = {"choices": [{"delta": {"content": "In stock"}}]}
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    fallback = MistralProvider("key", "model", transport=httpx.MockTransport(mistral_api))

    answer = await run_agent(
        session,
        RequestContext(user.id, org.id, user.role),
        conversation,
        "Is First Flush in stock?",
        FallbackProvider(primary, fallback),
    )

    assert answer.content == "In stock"
    assert (await session.scalar(select(ToolRun))).tool_name == "search_products"
    messages = sent[0]["messages"]
    call_id = messages[-2]["tool_calls"][0]["id"]
    assert [message["role"] for message in messages[-2:]] == ["assistant", "tool"]
    assert messages[-1]["tool_call_id"] == call_id
    assert len(call_id) == 9 and call_id.isalnum()
