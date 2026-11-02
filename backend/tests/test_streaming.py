import json
from contextlib import aclosing
from dataclasses import dataclass

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.gemini import GeminiProvider
from app.agents.runtime import stream_agent
from app.core.security import RequestContext, authenticated_subject
from app.db.database import get_session, get_session_factory
from app.db.models import (
    ChatMessage,
    Conversation,
    MessageRole,
    Order,
    OrderStatus,
    Organization,
    PendingAction,
    Product,
    ToolRun,
    User,
    UserRole,
)
from app.main import app, get_agent_provider

ANSWER = ProviderTurn(parts=[{"text": "Hello there"}], text="Hello there")


def tool_turn(name: str, arguments: dict | None = None) -> ProviderTurn:
    args = arguments or {}
    return ProviderTurn(
        parts=[{"functionCall": {"name": name, "args": args}}],
        tool_calls=[ToolCall(name, name, args)],
    )


@dataclass
class StreamingFake:
    """Each entry is one model call: text pieces, then the finished turn."""

    calls: list[list[str | ProviderTurn]]
    offered_tools: list[int] | None = None

    async def complete(self, **_):
        raise AssertionError("streaming providers are streamed")

    async def stream(self, *, system_prompt, events, tools):
        self.offered_tools = [*(self.offered_tools or []), len(tools)]
        for item in self.calls.pop(0):
            yield item


@pytest.fixture
async def shop(session):
    org = Organization(slug="himali-tea", name="Himali")
    session.add(org)
    await session.flush()
    user = User(org_id=org.id, email="buyer@test", auth_subject="auth-buyer")
    admin = User(
        org_id=org.id, email="admin@test", auth_subject="auth-admin", role=UserRole.ADMIN
    )
    session.add_all([user, admin])
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    session.add_all(
        [
            conversation,
            Product(
                org_id=org.id, sku="TEA-1", name="First Flush", category="black",
                price=500, stock=3,
            ),
        ]
    )
    await session.commit()
    return RequestContext(user.id, org.id, user.role), conversation, org, user


async def collect(session, context, conversation, provider, content="Hi", **kwargs):
    async with aclosing(
        stream_agent(session, context, conversation, content, provider, **kwargs)
    ) as stream:
        return [event async for event in stream]


async def test_text_arrives_in_pieces_and_is_saved_whole(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake([["Hello ", "there", ANSWER]])

    events = await collect(session, context, conversation, provider)

    assert [event.get("delta") for event in events[:2]] == ["Hello ", "there"]
    assert events[-1]["type"] == "done"
    assert events[-1]["message"].content == "Hello there"


async def test_tool_activity_is_reported_before_the_answer(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake([[tool_turn("search_products", {"query": "First"})], [ANSWER]])

    events = await collect(session, context, conversation, provider)

    assert events[0] == {"type": "activity", "label": "Checking stock…"}
    run = await session.scalar(select(ToolRun))
    assert run.tool_name == "search_products"


async def test_text_before_a_tool_call_is_withdrawn(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake(
        [["Let me check", tool_turn("search_products", {"query": "First"})], ["Done", ANSWER]]
    )

    events = await collect(session, context, conversation, provider)

    assert [event["type"] for event in events[:3]] == ["text", "text_reset", "activity"]


async def test_the_last_round_forces_an_answer(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake(
        [[tool_turn("search_products", {"query": "a"})], [tool_turn("list_my_orders")], [ANSWER]]
    )

    events = await collect(session, context, conversation, provider, max_tool_rounds=2)

    assert events[-1]["type"] == "done"
    assert provider.offered_tools[0] > 0
    assert provider.offered_tools[-1] == 0


async def test_stopping_keeps_the_question_and_the_partial_reply(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake([["Partial answer", " never seen", ANSWER]])

    async with aclosing(
        stream_agent(session, context, conversation, "Hi", provider)
    ) as stream:
        await anext(stream)

    rows = list(
        await session.scalars(select(ChatMessage).order_by(ChatMessage.created_at))
    )
    assert [(row.role, row.content) for row in rows] == [
        (MessageRole.USER, "Hi"),
        (MessageRole.ASSISTANT, "Partial answer"),
    ]


async def test_a_failure_keeps_the_question_and_tool_audit(session, shop):
    context, conversation, *_ = shop
    provider = StreamingFake([[tool_turn("search_products", {"query": "a"})]])

    with pytest.raises(IndexError):
        await collect(session, context, conversation, provider)

    await session.rollback()
    assert await session.scalar(select(ToolRun)) is not None
    assert (await session.scalar(select(ChatMessage))).content == "Hi"


@pytest.fixture
async def client(session):
    async def override_session():
        yield session

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def parse_events(body: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.strip()]


async def test_endpoint_streams_events_ending_in_the_saved_message(shop, client):
    _, conversation, *_ = shop
    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    app.dependency_overrides[get_agent_provider] = lambda: StreamingFake(
        [[tool_turn("search_products", {"query": "First"})], ["Hello there", ANSWER]]
    )

    response = await client.post(
        f"/api/conversations/{conversation.id}/messages", json={"content": "Hi"}
    )

    events = parse_events(response.text)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [event["type"] for event in events] == ["activity", "text", "done"]
    assert events[-1]["message"]["content"] == "Hello there"
    assert events[-1]["message"]["role"] == "assistant"


async def test_endpoint_reports_a_card_with_the_final_message(session, shop, client):
    _, conversation, *_ = shop
    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    arguments = {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": "Thamel"}
    app.dependency_overrides[get_agent_provider] = lambda: StreamingFake(
        [[tool_turn("create_order", arguments)], ["Please confirm", ANSWER]]
    )

    response = await client.post(
        f"/api/conversations/{conversation.id}/messages", json={"content": "Buy"}
    )

    action = parse_events(response.text)[-1]["message"]["actions"][0]
    assert action["type"] == "create_order"
    assert await session.scalar(select(Order)) is None
    assert await session.scalar(select(PendingAction)) is not None


async def test_endpoint_turns_a_model_failure_into_an_error_event(shop, client):
    _, conversation, *_ = shop
    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    app.dependency_overrides[get_agent_provider] = lambda: StreamingFake([])

    response = await client.post(
        f"/api/conversations/{conversation.id}/messages", json={"content": "Hi"}
    )

    assert parse_events(response.text) == [
        {"type": "error", "detail": "The agent could not complete the request"}
    ]


async def test_endpoint_refuses_someone_elses_conversation_before_streaming(
    session, shop, client
):
    _, conversation, org, _ = shop
    session.add(User(org_id=org.id, email="other@test", auth_subject="auth-other"))
    await session.commit()
    app.dependency_overrides[authenticated_subject] = lambda: "auth-other"
    app.dependency_overrides[get_agent_provider] = lambda: StreamingFake([])

    response = await client.post(
        f"/api/conversations/{conversation.id}/messages", json={"content": "Hi"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found"}


async def test_admin_order_list_is_org_scoped_and_admin_only(session, shop, client):
    _, _, org, user = shop
    other_org = Organization(slug="other", name="Other")
    session.add(other_org)
    await session.flush()
    stranger = User(org_id=other_org.id, email="stranger@test")
    session.add(stranger)
    await session.flush()
    mine = Order(
        org_id=org.id, customer_id=user.id, status=OrderStatus.PENDING, total=500,
        delivery_address="Thamel",
    )
    theirs = Order(
        org_id=other_org.id, customer_id=stranger.id, total=1, delivery_address="Elsewhere"
    )
    session.add_all([mine, theirs])
    await session.commit()

    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    assert (await client.get("/api/admin/orders")).status_code == 403

    app.dependency_overrides[authenticated_subject] = lambda: "auth-admin"
    rows = (await client.get("/api/admin/orders")).json()

    assert [row["id"] for row in rows] == [str(mine.id)]
    assert rows[0]["customer"] == "buyer@test"
    assert rows[0]["next_status"] == "confirmed"


async def test_gemini_stream_yields_text_then_the_turn_and_retries_when_busy():
    answers = [
        httpx.Response(503, json={"error": {}}),
        httpx.Response(
            200,
            content=(
                'data: {"candidates":[{"content":{"parts":[{"text":"Hel"}]}}]}\n\n'
                'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]}}]}\n\n'
            ),
        ),
    ]
    provider = GeminiProvider(
        "key",
        "model",
        retry_delay_seconds=0,
        transport=httpx.MockTransport(lambda _: answers.pop(0)),
    )

    items = [
        item
        async for item in provider.stream(
            system_prompt="Be brief",
            events=[AgentEvent(role="user", parts=[{"text": "Hi"}])],
            tools=[],
        )
    ]

    assert items[:2] == ["Hel", "lo"]
    assert items[2].text == "Hello"
    assert answers == []


async def test_gemini_stream_collects_function_calls():
    body = (
        'data: {"candidates":[{"content":{"parts":'
        '[{"functionCall":{"name":"list_services","args":{}}}]}}]}\n\n'
    )
    provider = GeminiProvider(
        "key", "model", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    )

    items = [
        item
        async for item in provider.stream(
            system_prompt="Be brief",
            events=[AgentEvent(role="user", parts=[{"text": "Hi"}])],
            tools=[],
        )
    ]

    assert len(items) == 1
    assert items[0].tool_calls[0].name == "list_services"
