import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.agents.actions import choose_slot, confirm_action, propose_order, propose_reschedule
from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.gemini import GeminiProvider
from app.agents.mistral import MistralProvider
from app.agents.providers import build_provider
from app.agents.runtime import run_agent
from app.core.security import RequestContext
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    OpeningHours,
    Organization,
    PendingAction,
    Practitioner,
    PractitionerService,
    Product,
    Service,
    User,
)

KATHMANDU = ZoneInfo("Asia/Kathmandu")


@dataclass
class Scripted:
    turns: list[ProviderTurn]

    async def complete(self, *, system_prompt, events, tools) -> ProviderTurn:
        return self.turns.pop(0)


def tool_turn(name: str, arguments: dict | None = None) -> ProviderTurn:
    args = arguments or {}
    return ProviderTurn(
        parts=[{"functionCall": {"name": name, "args": args}}],
        tool_calls=[ToolCall(name, name, args)],
    )


ANSWER = ProviderTurn(parts=[{"text": "Here you go"}], text="Here you go")


def local_time(days: int, hour: int) -> datetime:
    day = datetime.now(KATHMANDU).date() + timedelta(days=days)
    return datetime.combine(day, time(hour), KATHMANDU)


@pytest.fixture
async def studio(session):
    org = Organization(slug="lotus-wellness", name="Lotus")
    session.add(org)
    await session.flush()
    user = User(org_id=org.id, email="client@test")
    other = User(org_id=org.id, email="other@test")
    session.add_all([user, other])
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    service = Service(
        org_id=org.id, code="massage", name="Massage", category="massage",
        duration_minutes=60, price=Decimal("1000.00"),
    )
    practitioner = Practitioner(org_id=org.id, code="maya", name="Maya")
    session.add_all([conversation, service, practitioner])
    await session.flush()
    session.add(PractitionerService(practitioner_id=practitioner.id, service_id=service.id))
    for weekday in range(7):
        session.add(
            OpeningHours(org_id=org.id, weekday=weekday, opens_at=time(8), closes_at=time(18))
        )
    await session.commit()
    return SimpleNamespace(
        context=RequestContext(user.id, org.id, user.role),
        other=RequestContext(other.id, org.id, other.role),
        conversation=conversation,
        service=service,
        practitioner=practitioner,
    )


@pytest.fixture
async def store(session):
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
                price=Decimal("500.00"), stock=5,
            ),
        ]
    )
    await session.commit()
    return SimpleNamespace(context=RequestContext(user.id, org.id, user.role), conv=conversation)


async def book(session, studio, starts_at, context=None) -> Booking:
    context = context or studio.context
    booking = Booking(
        org_id=context.org_id,
        customer_id=context.user_id,
        service_id=studio.service.id,
        practitioner_id=studio.practitioner.id,
        starts_at=starts_at.astimezone(UTC),
        ends_at=(starts_at + timedelta(hours=1)).astimezone(UTC),
        status=BookingStatus.CONFIRMED,
        price=studio.service.price,
    )
    session.add(booking)
    await session.commit()
    return booking


def find_slots_turn(studio, day, **extra) -> ProviderTurn:
    return tool_turn(
        "find_available_slots",
        {
            "service_id": str(studio.service.id),
            "date_from": str(day),
            "date_to": str(day),
            **extra,
        },
    )


async def test_finding_slots_adds_a_pickable_card(session, studio):
    day = local_time(3, 8).date()
    provider = Scripted([find_slots_turn(studio, day), ANSWER])

    answer = await run_agent(session, studio.context, studio.conversation, "Free times?", provider)

    card = answer.cards[0]
    assert card["type"] == "slots"
    assert card["service_id"] == str(studio.service.id)
    assert card["slots"][0]["practitioner"] == "Maya"
    assert card["slots"][0]["label"].endswith("08:00")
    assert len(card["slots"]) <= 12


async def test_cards_stay_out_of_what_the_model_and_audit_see(session, studio):
    day = local_time(3, 8).date()
    seen: list[dict] = []

    class Recording(Scripted):
        async def complete(self, *, system_prompt, events, tools):
            seen.append(events[-1].parts[0])
            return await super().complete(
                system_prompt=system_prompt, events=events, tools=tools
            )

    await run_agent(
        session,
        studio.context,
        studio.conversation,
        "Free times?",
        Recording([find_slots_turn(studio, day), ANSWER]),
    )

    assert "card" not in seen[-1]["functionResponse"]["response"]


async def test_a_proposal_replaces_lookup_cards(session, store):
    arguments = {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": "Thamel"}
    provider = Scripted(
        [tool_turn("list_my_orders"), tool_turn("create_order", arguments), ANSWER]
    )

    answer = await run_agent(session, store.context, store.conv, "Buy", provider)

    assert answer.cards == []


async def test_order_status_and_confirmation_show_a_status_card(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conv.id,
        {"items": [{"sku": "TEA-1", "quantity": 2}], "delivery_address": "Thamel"},
    )
    reply, _ = await confirm_action(
        session, store.context, store.conv, uuid.UUID(proposal["action_id"])
    )

    card = reply.cards[0]
    assert card["type"] == "status"
    assert card["status"] == "pending"
    assert card["total"] == "Rs 1000.00"
    assert card["rows"][0]["label"] == "2 × First Flush"
    assert card["timeline"][0]["status"] == "pending"

    order_id = card["title"].split()[-1]
    assert len(order_id) == 8


async def test_bookings_card_lists_upcoming_first(session, studio):
    await book(session, studio, local_time(3, 9))
    provider = Scripted([tool_turn("list_my_bookings"), ANSWER])

    answer = await run_agent(session, studio.context, studio.conversation, "Mine?", provider)

    row = answer.cards[0]["rows"][0]
    assert row["label"] == "Massage"
    assert row["status"] == "confirmed"
    assert "Maya" in row["value"]


async def test_picking_a_slot_proposes_a_booking_without_changing_anything(session, studio):
    question, reply = await choose_slot(
        session,
        studio.context,
        studio.conversation,
        studio.service.id,
        studio.practitioner.id,
        local_time(3, 10).isoformat(),
    )

    assert question.content.endswith("10:00")
    assert "Confirm" in reply.content
    action = await session.scalar(select(PendingAction))
    assert action.message_id == reply.id
    assert await session.scalar(select(Booking)) is None

    await confirm_action(session, studio.context, studio.conversation, action.id)

    assert (await session.scalar(select(Booking))).status is BookingStatus.CONFIRMED


async def test_picking_a_taken_slot_offers_fresh_slots(session, studio):
    await book(session, studio, local_time(3, 10), studio.other)

    _, reply = await choose_slot(
        session,
        studio.context,
        studio.conversation,
        studio.service.id,
        studio.practitioner.id,
        local_time(3, 10).isoformat(),
    )

    assert "no longer available" in reply.content
    assert reply.cards[0]["title"] == "Nearest free times"
    assert await session.scalar(select(PendingAction)) is None


async def test_picking_a_slot_for_an_existing_booking_moves_it(session, studio):
    booking = await book(session, studio, local_time(3, 9))

    await choose_slot(
        session,
        studio.context,
        studio.conversation,
        studio.service.id,
        studio.practitioner.id,
        local_time(3, 15).isoformat(),
        booking.id,
    )

    action = await session.scalar(select(PendingAction))
    assert action.action_type == "reschedule_booking"
    await confirm_action(session, studio.context, studio.conversation, action.id)
    await session.refresh(booking)
    assert booking.starts_at.replace(tzinfo=UTC).astimezone(KATHMANDU).hour == 15


async def test_nobody_can_pick_a_slot_to_move_someone_elses_booking(session, studio):
    theirs = await book(session, studio, local_time(3, 9), studio.other)

    _, reply = await choose_slot(
        session,
        studio.context,
        studio.conversation,
        studio.service.id,
        studio.practitioner.id,
        local_time(3, 15).isoformat(),
        theirs.id,
    )

    assert "not found" in reply.content
    assert await session.scalar(select(PendingAction)) is None


async def test_a_conflicting_reschedule_returns_a_card_for_the_same_booking(session, studio):
    mine = await book(session, studio, local_time(3, 9))
    await book(session, studio, local_time(3, 14), studio.other)

    result = await propose_reschedule(
        session,
        studio.context,
        studio.conversation.id,
        {"booking_id": str(mine.id), "starts_at": local_time(3, 14).isoformat()},
    )

    assert result["card"]["booking_id"] == str(mine.id)


def llm_settings(**values) -> SimpleNamespace:
    defaults = {
        "llm_provider": "gemini",
        "llm_model": None,
        "llm_api_key": None,
        "llm_fallback_provider": None,
        "llm_fallback_model": None,
        "llm_fallback_api_key": None,
        "llm_fallback_after_seconds": 10,
    }
    return SimpleNamespace(**{**defaults, **values})


@pytest.mark.parametrize(
    ("name", "kind", "default_model"),
    [
        ("gemini", GeminiProvider, "gemini-3.5-flash-lite"),
        ("mistral", MistralProvider, "ministral-14b-latest"),
        ("MISTRAL", MistralProvider, "ministral-14b-latest"),
    ],
)
def test_provider_is_chosen_by_environment(name, kind, default_model):
    provider = build_provider(llm_settings(llm_provider=name, llm_api_key="key"))

    assert isinstance(provider, kind)
    assert provider.model == default_model


def test_chosen_model_wins_and_missing_config_gives_no_provider():
    chosen = build_provider(
        llm_settings(llm_provider="mistral", llm_model="mistral-large", llm_api_key="key")
    )
    assert chosen.model == "mistral-large"
    for broken in (
        llm_settings(llm_provider="mistral"),
        llm_settings(llm_provider="other", llm_api_key="key"),
    ):
        assert build_provider(broken) is None


def mistral(body_or_handler) -> MistralProvider:
    handler = (
        body_or_handler
        if callable(body_or_handler)
        else lambda _: httpx.Response(200, content=body_or_handler)
    )
    return MistralProvider(
        "key", "ministral-14b-latest", retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def sse(*chunks) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


USER_ONLY = [AgentEvent(role="user", parts=[{"text": "Hi"}])]


async def collect(provider, events=USER_ONLY, tools=()):
    return [
        item
        async for item in provider.stream(
            system_prompt="Be brief", events=list(events), tools=list(tools)
        )
    ]


async def test_mistral_streams_text_then_the_turn():
    body = sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": [{"type": "text", "text": "lo"}]}}]},
    )

    items = await collect(mistral(body))

    assert items[:2] == ["Hel", "lo"]
    assert items[2].text == "Hello"
    assert items[2].tool_calls == []


async def test_mistral_collects_a_whole_tool_call():
    body = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "abc123xyz",
                                "function": {
                                    "name": "search_products",
                                    "arguments": json.dumps({"query": "a"}),
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )

    turn = (await collect(mistral(body)))[-1]

    assert turn.tool_calls == [ToolCall("abc123xyz", "search_products", {"query": "a"})]
    assert turn.parts[0]["functionCall"]["id"] == "abc123xyz"


async def test_mistral_joins_a_tool_call_spread_over_events():
    fragment = lambda **call: {"choices": [{"delta": {"tool_calls": [call]}}]}  # noqa: E731
    body = sse(
        fragment(id="abc123xyz", function={"name": "search_products", "arguments": '{"qu'}),
        fragment(function={"arguments": 'ery":"a"}'}),
    )

    turn = (await collect(mistral(body)))[-1]

    assert turn.tool_calls == [ToolCall("abc123xyz", "search_products", {"query": "a"})]


async def test_mistral_translates_the_whole_history_and_tools():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "Done"}}]}))

    events = [
        AgentEvent(role="user", parts=[{"text": "Tea?"}]),
        AgentEvent(
            role="model",
            parts=[{"functionCall": {"id": "abc", "name": "search_products", "args": {"q": 1}}}],
        ),
        AgentEvent(
            role="user",
            parts=[
                {"functionResponse": {"id": "abc", "name": "search_products", "response": {"n": 2}}}
            ],
        ),
    ]
    tool = {"name": "search_products", "description": "d", "parameters": {"type": "object"}}

    await collect(mistral(handler), events, [tool])

    messages = sent[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == {"q": 1}
    assert messages[3]["tool_call_id"] == messages[2]["tool_calls"][0]["id"]
    assert json.loads(messages[3]["content"]) == {"n": 2}
    assert sent[0]["tools"] == [{"type": "function", "function": tool}]
    assert sent[0]["stream"] is True


async def test_mistral_omits_tools_on_the_forced_answer_round():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "Done"}}]}))

    await collect(mistral(handler))

    assert "tools" not in sent[0]


async def test_mistral_waits_and_retries_when_rate_limited():
    answers = [
        httpx.Response(429, headers={"retry-after": "0"}, json={"message": "slow down"}),
        httpx.Response(200, content=sse({"choices": [{"delta": {"content": "Hi"}}]})),
    ]

    items = await collect(mistral(lambda _: answers.pop(0)))

    assert items[0] == "Hi"
    assert answers == []


async def test_mistral_does_not_retry_a_bad_key():
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(401, json={"message": "Unauthorized"})

    with pytest.raises(httpx.HTTPStatusError):
        await collect(mistral(handler))

    assert calls == [1]


async def test_both_providers_work_through_the_agent_loop(session, store):
    def handler(_):
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "Namaste"}}]}))

    answer = await run_agent(
        session, store.context, store.conv, "Hi", mistral(handler)
    )

    assert answer.content == "Namaste"
