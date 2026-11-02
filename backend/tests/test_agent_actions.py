import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.agents.actions import (
    ActionError,
    actions_by_message,
    confirm_action,
    dismiss_action,
    propose_booking,
    propose_booking_cancel,
    propose_order,
    propose_order_cancel,
    propose_order_update,
    propose_reorder,
    propose_reschedule,
)
from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.registry import registry_for
from app.agents.runtime import run_agent
from app.core.security import RequestContext, authenticated_subject
from app.db.database import get_session
from app.db.models import (
    Booking,
    BookingStatus,
    ChatMessage,
    Conversation,
    OpeningHours,
    Order,
    OrderStatus,
    Organization,
    PendingAction,
    PendingActionStatus,
    Practitioner,
    PractitionerService,
    Product,
    Service,
    ToolRun,
    User,
)
from app.main import app
from app.services.bookings import nearest_slots

KATHMANDU = ZoneInfo("Asia/Kathmandu")
ADDRESS = "Thamel, Kathmandu"


@dataclass
class Store:
    context: RequestContext
    conversation: Conversation
    tea: Product
    other_tea: Product


@dataclass
class Studio:
    context: RequestContext
    other: RequestContext
    conversation: Conversation
    service: Service
    practitioner: Practitioner


@dataclass
class ScriptedProvider:
    turns: list[ProviderTurn]

    async def complete(
        self, *, system_prompt: str, events: list[AgentEvent], tools: list[dict]
    ) -> ProviderTurn:
        return self.turns.pop(0)


def action_id(proposal: dict) -> uuid.UUID:
    return uuid.UUID(proposal["action_id"])


def tool_turn(name: str, arguments: dict | None = None) -> ProviderTurn:
    args = arguments or {}
    return ProviderTurn(
        parts=[{"functionCall": {"name": name, "args": args}}],
        tool_calls=[ToolCall(name, name, args)],
    )


def text_turn(text: str = "Done") -> ProviderTurn:
    return ProviderTurn(parts=[{"text": text}], text=text)


async def customer(session, org, email, subject=None) -> tuple[User, RequestContext]:
    user = User(org_id=org.id, email=email, auth_subject=subject)
    session.add(user)
    await session.flush()
    return user, RequestContext(user.id, org.id, user.role)


@pytest.fixture
async def store(session) -> Store:
    org = Organization(slug="himali-tea", name="Himali")
    session.add(org)
    await session.flush()
    user, context = await customer(session, org, "buyer@test", "auth-buyer")
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    tea = Product(
        org_id=org.id, sku="TEA-1", name="First Flush", category="black",
        price=Decimal("500.00"), stock=3,
    )
    other = Product(
        org_id=org.id, sku="TEA-2", name="Second Flush", category="black",
        price=Decimal("400.00"), stock=9,
    )
    session.add_all([conversation, tea, other])
    await session.commit()
    return Store(context, conversation, tea, other)


@pytest.fixture
async def studio(session) -> Studio:
    org = Organization(slug="lotus-wellness", name="Lotus")
    session.add(org)
    await session.flush()
    user, context = await customer(session, org, "client@test")
    _, other = await customer(session, org, "other@test")
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
    return Studio(context, other, conversation, service, practitioner)


def local_time(days: int, hour: int, minute: int = 0) -> datetime:
    day = datetime.now(KATHMANDU).date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute), KATHMANDU)


async def place_order(session, store: Store, quantity: int = 2) -> Order:
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": quantity}], "delivery_address": ADDRESS},
    )
    await confirm_action(
        session, store.context, store.conversation, action_id(proposal)
    )
    return await session.scalar(select(Order).order_by(Order.created_at.desc()).limit(1))


async def book(session, studio: Studio, starts_at: datetime, context=None) -> Booking:
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


async def test_a_proposal_changes_nothing_until_it_is_confirmed(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "tea-1", "quantity": 2}], "delivery_address": ADDRESS},
    )

    assert proposal["status"] == "awaiting_confirmation"
    assert proposal["summary"]["total"] == "Rs 1000.00"
    assert await session.scalar(select(Order)) is None
    assert store.tea.stock == 3

    reply, action = await confirm_action(
        session, store.context, store.conversation, action_id(proposal)
    )

    order = await session.scalar(select(Order))
    await session.refresh(store.tea)
    assert action.status is PendingActionStatus.CONFIRMED
    assert order.customer_id == store.context.user_id
    assert order.total == Decimal("1000.00")
    assert store.tea.stock == 1
    assert str(order.id) in reply.content


async def test_a_confirmation_cannot_be_used_twice(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )
    await confirm_action(session, store.context, store.conversation, action_id(proposal))

    with pytest.raises(ActionError) as error:
        await confirm_action(session, store.context, store.conversation, action_id(proposal))

    assert error.value.status_code == 409
    await session.refresh(store.tea)
    assert store.tea.stock == 2


async def test_an_expired_proposal_cannot_be_confirmed(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )
    action = await session.get(PendingAction, action_id(proposal))
    action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    with pytest.raises(ActionError, match="expired"):
        await confirm_action(session, store.context, store.conversation, action.id)

    assert await session.scalar(select(Order)) is None


async def test_a_new_proposal_replaces_the_old_one(session, store):
    arguments = {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS}
    first = await propose_order(session, store.context, store.conversation.id, arguments)
    await propose_order(session, store.context, store.conversation.id, arguments)

    with pytest.raises(ActionError, match="already cancelled"):
        await confirm_action(session, store.context, store.conversation, action_id(first))


async def test_price_change_after_the_proposal_fails_safely(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )
    store.tea.price = Decimal("600.00")
    await session.commit()

    reply, action = await confirm_action(
        session, store.context, store.conversation, action_id(proposal)
    )

    assert action.status is PendingActionStatus.CANCELLED
    assert "nothing was changed" in reply.content
    assert await session.scalar(select(Order)) is None


async def test_short_stock_offers_alternatives(session, store):
    result = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 4}], "delivery_address": ADDRESS},
    )

    assert "only 3" in result["error"]
    assert [item["sku"] for item in result["alternatives"]] == ["TEA-2"]


async def test_an_edit_can_keep_the_stock_the_order_already_holds(session, store):
    order = await place_order(session, store, quantity=3)
    await session.refresh(store.tea)
    assert store.tea.stock == 0

    proposal = await propose_order_update(
        session,
        store.context,
        store.conversation.id,
        {"order_id": str(order.id), "delivery_address": "New address, Patan"},
    )
    await confirm_action(session, store.context, store.conversation, action_id(proposal))

    await session.refresh(order)
    await session.refresh(store.tea)
    assert order.delivery_address == "New address, Patan"
    assert store.tea.stock == 0


@pytest.mark.parametrize("propose", [propose_order_update, propose_order_cancel])
async def test_non_pending_order_is_refused_with_its_status(session, store, propose):
    order = await place_order(session, store)
    order.status = OrderStatus.SHIPPED
    await session.commit()

    result = await propose(
        session, store.context, store.conversation.id, {"order_id": str(order.id)}
    )

    assert "shipped" in result["error"]
    assert "action_id" not in result


async def test_reorder_notes_price_changes(session, store):
    order = await place_order(session, store, quantity=1)
    store.tea.price = Decimal("550.00")
    await session.commit()

    result = await propose_reorder(
        session, store.context, store.conversation.id, {"order_id": str(order.id)}
    )

    assert result["price_changes"] == ["First Flush was Rs 500.00 and is now Rs 550.00"]
    assert result["summary"]["total"] == "Rs 550.00"


async def test_another_customers_order_cannot_be_proposed_on(session, store):
    order = await place_order(session, store)
    org = await session.get(Organization, store.context.org_id)
    user, stranger = await customer(session, org, "stranger@test")
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Mine")
    session.add(conversation)
    await session.commit()

    result = await propose_order_cancel(
        session, stranger, conversation.id, {"order_id": str(order.id)}
    )

    assert result["error"] == "Order not found"


async def test_another_customer_cannot_confirm_my_action(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )
    org = await session.get(Organization, store.context.org_id)
    _, stranger = await customer(session, org, "stranger@test")

    with pytest.raises(ActionError) as error:
        await confirm_action(session, stranger, store.conversation, action_id(proposal))

    assert error.value.status_code == 404
    assert await session.scalar(select(Order)) is None


async def test_dismissing_changes_nothing(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )

    _, action = await dismiss_action(
        session, store.context, store.conversation, action_id(proposal)
    )

    assert action.status is PendingActionStatus.CANCELLED
    assert await session.scalar(select(Order)) is None


async def test_booking_is_summarised_in_local_time_then_confirmed(session, studio):
    starts_at = local_time(3, 10)
    proposal = await propose_booking(
        session,
        studio.context,
        studio.conversation.id,
        {
            "service_id": str(studio.service.id),
            "practitioner_id": str(studio.practitioner.id),
            "starts_at": starts_at.isoformat(),
        },
    )

    lines = {line["label"]: line["value"] for line in proposal["summary"]["lines"]}
    assert lines["When"].endswith("10:00")
    assert lines["Practitioner"] == "Maya"
    assert proposal["summary"]["total"] == "Rs 1000.00"
    assert await session.scalar(select(Booking)) is None

    await confirm_action(session, studio.context, studio.conversation, action_id(proposal))

    booking = await session.scalar(select(Booking))
    assert booking.customer_id == studio.context.user_id
    assert booking.status is BookingStatus.CONFIRMED


async def test_booking_past_midnight_is_refused(session, studio):
    result = await propose_booking(
        session,
        studio.context,
        studio.conversation.id,
        {
            "service_id": str(studio.service.id),
            "practitioner_id": str(studio.practitioner.id),
            "starts_at": local_time(3, 23, 30).isoformat(),
        },
    )

    assert "opening hours" in result["error"]


async def test_conflicting_reschedule_offers_the_nearest_free_slots(session, studio):
    mine = await book(session, studio, local_time(3, 9))
    await book(session, studio, local_time(3, 14), studio.other)

    result = await propose_reschedule(
        session,
        studio.context,
        studio.conversation.id,
        {"booking_id": str(mine.id), "starts_at": local_time(3, 14).isoformat()},
    )

    assert "no longer available" in result["error"]
    nearest = [slot["local_start"][11:16] for slot in result["nearest_free_slots"]]
    assert set(nearest[:2]) == {"13:00", "15:00"}
    assert await session.scalar(select(PendingAction)) is None


async def test_nearest_slots_can_overlap_the_booking_being_moved(session, studio):
    mine = await book(session, studio, local_time(3, 10))

    slots = await nearest_slots(
        session,
        studio.context,
        studio.service.id,
        studio.practitioner.id,
        local_time(3, 10, 30),
        exclude_booking_id=mine.id,
    )

    assert slots[0]["local_start"][11:16] == "10:30"


async def test_reschedule_moves_the_booking_on_confirmation(session, studio):
    booking = await book(session, studio, local_time(3, 9))
    proposal = await propose_reschedule(
        session,
        studio.context,
        studio.conversation.id,
        {"booking_id": str(booking.id), "starts_at": local_time(3, 15).isoformat()},
    )
    assert "free" in proposal["summary"]["note"]

    await confirm_action(session, studio.context, studio.conversation, action_id(proposal))

    await session.refresh(booking)
    starts_at = booking.starts_at.replace(tzinfo=UTC).astimezone(KATHMANDU)
    assert starts_at.hour == 15
    assert booking.late_fee == Decimal("0")


async def test_late_cancellation_shows_and_charges_the_fee(session, studio):
    soon = datetime.now(UTC) + timedelta(hours=5)
    booking = await book(session, studio, soon.astimezone(KATHMANDU))

    proposal = await propose_booking_cancel(
        session, studio.context, studio.conversation.id, {"booking_id": str(booking.id)}
    )
    assert "Rs 500.00" in proposal["summary"]["note"]

    reply, _ = await confirm_action(
        session, studio.context, studio.conversation, action_id(proposal)
    )

    await session.refresh(booking)
    assert booking.status is BookingStatus.CANCELLED
    assert booking.late_fee == Decimal("500.00")
    assert "Rs 500.00" in reply.content


async def test_early_cancellation_is_free(session, studio):
    booking = await book(session, studio, local_time(5, 10))
    proposal = await propose_booking_cancel(
        session, studio.context, studio.conversation.id, {"booking_id": str(booking.id)}
    )

    await confirm_action(session, studio.context, studio.conversation, action_id(proposal))

    await session.refresh(booking)
    assert booking.late_fee == Decimal("0")


async def test_past_booking_cannot_be_cancelled(session, studio):
    booking = await book(session, studio, local_time(-2, 10))

    result = await propose_booking_cancel(
        session, studio.context, studio.conversation.id, {"booking_id": str(booking.id)}
    )

    assert "already started" in result["error"]


async def test_both_agents_expose_every_tool_in_the_brief(session, store):
    _, registry = await registry_for(session, store.context)
    assert registry.names == {
        "search_knowledge",
        "search_products",
        "create_order",
        "update_order",
        "cancel_order",
        "get_order_status",
        "list_my_orders",
        "reorder",
    }


async def test_lotus_agent_tool_list(session, studio):
    _, registry = await registry_for(session, studio.context)
    assert registry.names == {
        "search_knowledge",
        "list_services",
        "get_opening_hours",
        "find_available_slots",
        "create_booking",
        "reschedule_booking",
        "cancel_booking",
        "list_my_bookings",
    }


async def test_model_has_no_tool_that_can_confirm(session, store, studio):
    for context in (store.context, studio.context):
        _, registry = await registry_for(session, context)
        assert not any("confirm" in name for name in registry.names)


async def test_lotus_list_services_runs_through_the_agent_loop(session, studio):
    provider = ScriptedProvider([tool_turn("list_services"), text_turn("We offer massage.")])

    answer = await run_agent(session, studio.context, studio.conversation, "Services?", provider)

    run = await session.scalar(select(ToolRun))
    assert answer.content == "We offer massage."
    assert run.status == "completed"
    assert run.output_data["services"][0]["name"] == "Massage"


async def test_agent_create_order_returns_a_card_and_places_nothing(session, store):
    provider = ScriptedProvider(
        [
            tool_turn(
                "create_order",
                {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
            ),
            text_turn("Please press Confirm."),
        ]
    )

    answer = await run_agent(session, store.context, store.conversation, "Buy tea", provider)

    cards = await actions_by_message(session, store.conversation.id)
    assert cards[answer.id][0]["type"] == "create_order"
    assert cards[answer.id][0]["status"] == "pending"
    assert await session.scalar(select(Order)) is None


async def test_a_turns_messages_keep_their_order(session, store):
    for number in range(5):
        provider = ScriptedProvider([text_turn(f"answer {number}")])
        await run_agent(session, store.context, store.conversation, f"question {number}", provider)

    rows = await session.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == store.conversation.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )

    assert [row.content for row in rows] == [
        text for number in range(5) for text in (f"question {number}", f"answer {number}")
    ]


async def test_confirm_endpoint_acts_only_for_the_signed_in_owner(session, store):
    proposal = await propose_order(
        session,
        store.context,
        store.conversation.id,
        {"items": [{"sku": "TEA-1", "quantity": 1}], "delivery_address": ADDRESS},
    )
    await session.commit()
    org = await session.get(Organization, store.context.org_id)
    await customer(session, org, "stranger@test", "auth-stranger")
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    path = (
        f"/api/conversations/{store.conversation.id}/actions/{action_id(proposal)}/confirm"
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            app.dependency_overrides[authenticated_subject] = lambda: "auth-stranger"
            refused = await http.post(path)
            app.dependency_overrides[authenticated_subject] = lambda: "auth-buyer"
            accepted = await http.post(path)
    finally:
        app.dependency_overrides.clear()

    assert refused.status_code == 404
    assert accepted.status_code == 200
    assert accepted.json()["action"]["status"] == "confirmed"
    assert (await session.scalar(select(Order))).total == Decimal("500.00")
