import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.contracts import ProviderTurn
from app.agents.suggestions import suggest_next_message
from app.core.security import authenticated_subject
from app.db.database import get_session
from app.db.models import (
    ChatMessage,
    Conversation,
    MessageRole,
    Organization,
    PendingAction,
    PendingActionStatus,
    User,
)
from app.main import app, get_suggestion_provider


@dataclass
class Fake:
    text: str = "Do you ship to Pokhara?"
    error: Exception | None = None
    silent: bool = False
    calls: list[dict] = field(default_factory=list)

    async def complete(self, *, system_prompt, events, tools):
        text = events[0].parts[0]["text"]
        self.calls.append({"system": system_prompt, "text": text, "tools": tools})
        if self.silent:
            await asyncio.sleep(60)
        if self.error:
            raise self.error
        return ProviderTurn(parts=[{"text": self.text}], text=self.text)


@pytest.fixture
async def chat(session):
    org = Organization(slug="himali-tea", name="Himali Tea House")
    session.add(org)
    await session.flush()
    user = User(org_id=org.id, email="buyer@test", auth_subject="auth-buyer")
    session.add(user)
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    session.add(conversation)
    await session.flush()
    session.add(ChatMessage(
        conversation_id=conversation.id, role=MessageRole.USER, content="Do you ship?", citations=[]
    ))
    await session.flush()
    session.add(ChatMessage(
        conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="Yes.", citations=[]
    ))
    await session.commit()
    return conversation, user


async def test_it_suggests_from_the_recent_conversation(session, chat):
    conversation, _ = chat
    provider = Fake()

    suggestion = await suggest_next_message(session, conversation, provider)

    assert suggestion == "Do you ship to Pokhara?"
    assert provider.calls[0]["text"] == "user: Do you ship?\nassistant: Yes."
    assert provider.calls[0]["tools"] == []
    assert "Himali Tea House" in provider.calls[0]["system"]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('"How much is it?"', "How much is it?"),
        ("**How much is it?**", "How much is it?"),
        ("How much is it?\nOr something else", "How much is it?"),
        ("NONE", None),
        ("none.", None),
        ("   ", None),
        ("word " * 40, None),
    ],
)
async def test_replies_are_cleaned_up(session, chat, reply, expected):
    conversation, _ = chat

    assert await suggest_next_message(session, conversation, Fake(text=reply)) == expected


async def test_a_pending_confirm_card_means_no_suggestion_and_no_model_call(session, chat):
    conversation, user = chat
    session.add(
        PendingAction(
            org_id=conversation.org_id,
            user_id=user.id,
            conversation_id=conversation.id,
            action_type="create_order",
            payload={"params": {}, "summary": {}},
            status=PendingActionStatus.PENDING,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await session.commit()
    provider = Fake()

    assert await suggest_next_message(session, conversation, provider) is None
    assert provider.calls == []


async def test_it_waits_for_the_assistant_to_have_answered(session, chat):
    conversation, _ = chat
    session.add(ChatMessage(
        conversation_id=conversation.id, role=MessageRole.USER, content="Hello?", citations=[]
    ))
    await session.commit()
    provider = Fake()

    assert await suggest_next_message(session, conversation, provider) is None
    assert provider.calls == []


async def test_a_model_failure_just_means_no_suggestion(session, chat):
    conversation, _ = chat

    provider = Fake(error=RuntimeError("503"))

    assert await suggest_next_message(session, conversation, provider) is None


async def test_a_slow_model_is_given_up_on(session, chat, monkeypatch):
    conversation, _ = chat
    monkeypatch.setattr("app.agents.suggestions.TIME_LIMIT_SECONDS", 0.05)

    assert await suggest_next_message(session, conversation, Fake(silent=True)) is None


@pytest.fixture
async def client(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def test_endpoint_returns_a_suggestion_for_the_owner(chat, client):
    conversation, _ = chat
    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    app.dependency_overrides[get_suggestion_provider] = lambda: Fake()

    response = await client.post(f"/api/conversations/{conversation.id}/suggestion")

    assert response.json() == {"suggestion": "Do you ship to Pokhara?"}


async def test_endpoint_refuses_someone_elses_conversation(session, chat, client):
    conversation, user = chat
    session.add(User(org_id=user.org_id, email="other@test", auth_subject="auth-other"))
    await session.commit()
    app.dependency_overrides[authenticated_subject] = lambda: "auth-other"
    provider = Fake()
    app.dependency_overrides[get_suggestion_provider] = lambda: provider

    response = await client.post(f"/api/conversations/{conversation.id}/suggestion")

    assert response.status_code == 404
    assert provider.calls == []


async def test_endpoint_is_rate_limited_separately_from_chat(chat, client, monkeypatch):
    conversation, _ = chat
    app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
    app.dependency_overrides[get_suggestion_provider] = lambda: Fake()
    from app.core.rate_limit import SlidingWindowLimiter

    monkeypatch.setattr("app.main.suggestion_limiter", SlidingWindowLimiter(maximum=1))

    path = f"/api/conversations/{conversation.id}/suggestion"
    first = await client.post(path)
    second = await client.post(path)

    assert first.status_code == 200
    assert second.status_code == 429
