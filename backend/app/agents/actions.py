"""Write tools propose a change here; it only happens when the customer confirms it.

The model can create a proposal but has no tool that confirms one, so nothing it reads or is
told can place an order or move a booking. The confirm endpoint re-runs every business rule
inside the service functions, so a stale proposal fails safely.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cards import booking_card, order_card, slots_card
from app.core.security import RequestContext
from app.db.models import (
    Booking,
    BookingStatus,
    ChatMessage,
    Conversation,
    MessageRole,
    Organization,
    PendingAction,
    PendingActionStatus,
    Practitioner,
    Service,
)
from app.services.bookings import (
    BookingRuleError,
    as_utc,
    cancel_booking,
    create_booking,
    late_fee_for,
    nearest_slots,
    reschedule_booking,
    validate_slot,
)
from app.services.orders import (
    OrderRuleError,
    RequestedItem,
    alternatives_for,
    cancel_pending_order,
    create_order,
    order_detail,
    quote_order,
    quote_order_change,
    reorder,
    update_pending_order,
)

ACTION_LIFETIME = timedelta(minutes=15)
# From the shipping policy (seed/himali-tea/kb/shipping.md). Collected with cash on delivery.
DELIVERY_NOTE = (
    "Total excludes delivery: Rs 150 inside the Kathmandu valley, Rs 300 outside it, "
    "and free on orders of Rs 3,000 or more."
)
MAX_ITEMS = 50
MAX_QUANTITY = 100


class ActionError(ValueError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


async def _propose(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    action_type: str,
    params: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    # Only the latest proposal can be confirmed, so an old card can't act on a changed mind.
    await session.execute(
        update(PendingAction)
        .where(
            PendingAction.conversation_id == conversation_id,
            PendingAction.status == PendingActionStatus.PENDING,
        )
        .values(status=PendingActionStatus.CANCELLED)
    )
    action = PendingAction(
        org_id=context.org_id,
        user_id=context.user_id,
        conversation_id=conversation_id,
        action_type=action_type,
        payload={"params": params, "summary": summary},
        expires_at=datetime.now(UTC) + ACTION_LIFETIME,
    )
    session.add(action)
    await session.flush()
    return {
        "status": "awaiting_confirmation",
        "action_id": str(action.id),
        "summary": summary,
        "changed_anything": False,
    }


def _requested_items(raw: Any) -> list[RequestedItem]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ITEMS:
        raise OrderRuleError(f"An order needs between 1 and {MAX_ITEMS} items")
    items = [RequestedItem(str(item["sku"]), int(item["quantity"])) for item in raw]
    if any(item.quantity > MAX_QUANTITY for item in items):
        raise OrderRuleError(f"An item's quantity can't be more than {MAX_QUANTITY}")
    return items


def _detail_items(detail: dict[str, Any]) -> list[RequestedItem]:
    return [RequestedItem(str(item["sku"]), int(item["quantity"])) for item in detail["items"]]


def _address(raw: Any) -> str:
    address = str(raw or "").strip()
    if not 3 <= len(address) <= 500:
        raise OrderRuleError("A delivery address between 3 and 500 characters is required")
    return address


def _plain_items(items: list[RequestedItem]) -> list[dict[str, Any]]:
    return [{"sku": item.sku, "quantity": item.quantity} for item in items]


def _order_lines(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": f"{line['quantity']} × {line['name']}", "value": f"Rs {line['line_total']}"}
        for line in lines
    ]


async def _order_refusal(
    session: AsyncSession, context: RequestContext, exc: OrderRuleError
) -> dict[str, Any]:
    refusal: dict[str, Any] = {"error": str(exc)}
    if exc.short_sku:
        refusal["alternatives"] = await alternatives_for(session, context, exc.short_sku)
    return refusal


async def propose_order(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        items = _requested_items(arguments.get("items"))
        address = _address(arguments.get("delivery_address"))
        lines, total, _ = await quote_order(session, context, items)
    except OrderRuleError as exc:
        return await _order_refusal(session, context, exc)
    return await _propose(
        session,
        context,
        conversation_id,
        "create_order",
        {
            "items": _plain_items(items),
            "delivery_address": address,
            "expected_total": str(total),
        },
        {
            "title": "Confirm your order",
            "lines": [*_order_lines(lines), {"label": "Deliver to", "value": address}],
            "total": f"Rs {total}",
            "note": f"Payment is cash on delivery. {DELIVERY_NOTE}",
        },
    )


async def propose_order_update(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        order_id = uuid.UUID(str(arguments["order_id"]))
        detail = await order_detail(session, context, order_id)
        items = (
            _requested_items(arguments["items"])
            if arguments.get("items")
            else _detail_items(detail)
        )
        address = _address(arguments.get("delivery_address") or detail["delivery_address"])
        lines, total = await quote_order_change(session, context, order_id, items)
    except OrderRuleError as exc:
        return await _order_refusal(session, context, exc)
    return await _propose(
        session,
        context,
        conversation_id,
        "update_order",
        {
            "order_id": str(order_id),
            "items": _plain_items(items),
            "delivery_address": address,
            "expected_total": str(total),
        },
        {
            "title": "Confirm the changes to your order",
            "lines": [*_order_lines(lines), {"label": "Deliver to", "value": address}],
            "total": f"Rs {total}",
            "note": f"The order total was Rs {detail['total']}. {DELIVERY_NOTE}",
        },
    )


async def propose_order_cancel(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        order_id = uuid.UUID(str(arguments["order_id"]))
        detail = await order_detail(session, context, order_id)
    except OrderRuleError as exc:
        return await _order_refusal(session, context, exc)
    if detail["status"] != "pending":
        return {
            "error": f"A {detail['status']} order cannot be cancelled",
            "current_status": detail["status"],
        }
    return await _propose(
        session,
        context,
        conversation_id,
        "cancel_order",
        {"order_id": str(order_id)},
        {
            "title": "Cancel this order?",
            "lines": [
                {"label": f"{item['quantity']} × {item['name']}", "value": ""}
                for item in detail["items"]
            ],
            "total": f"Rs {detail['total']}",
            "note": "The items go back into stock.",
        },
    )


def _price_changes(detail: dict[str, Any], lines: list[dict[str, Any]]) -> list[str]:
    previous = {item["sku"]: item["unit_price"] for item in detail["items"]}
    return [
        f"{line['name']} was Rs {previous[line['sku']]} and is now Rs {line['unit_price']}"
        for line in lines
        if Decimal(str(previous[line["sku"]])) != Decimal(str(line["unit_price"]))
    ]


async def propose_reorder(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        source_id = uuid.UUID(str(arguments["order_id"]))
        detail = await order_detail(session, context, source_id)
        lines, total, _ = await quote_order(session, context, _detail_items(detail))
    except OrderRuleError as exc:
        return await _order_refusal(session, context, exc)
    changes = _price_changes(detail, lines)
    result = await _propose(
        session,
        context,
        conversation_id,
        "reorder",
        {"order_id": str(source_id), "expected_total": str(total)},
        {
            "title": "Order the same again?",
            "lines": [
                *_order_lines(lines),
                {"label": "Deliver to", "value": detail["delivery_address"]},
            ],
            "total": f"Rs {total}",
            "note": (
                f"{'; '.join(changes) if changes else 'Prices are unchanged since last time'}. "
                f"{DELIVERY_NOTE}"
            ),
        },
    )
    return {**result, "price_changes": changes}


def _parse_start(raw: Any, organization: Organization) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise BookingRuleError("starts_at must be an ISO 8601 date and time") from exc
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(organization.timezone))


def _when(value: datetime, organization: Organization) -> str:
    local = as_utc(value).astimezone(ZoneInfo(organization.timezone))
    return local.strftime("%a %d %b %Y, %H:%M")


async def _slot_refusal(
    session: AsyncSession,
    context: RequestContext,
    exc: BookingRuleError,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID,
    around: datetime,
    exclude_booking_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    refusal: dict[str, Any] = {"error": str(exc)}
    if exc.status_code == 409:
        nearest = await nearest_slots(
            session,
            context,
            service_id,
            practitioner_id,
            around,
            exclude_booking_id=exclude_booking_id,
        )
        refusal["nearest_free_slots"] = nearest
        if nearest:
            refusal["card"] = slots_card(
                nearest,
                str(service_id),
                str(exclude_booking_id) if exclude_booking_id else None,
                title="Nearest free times",
            )
    return refusal


async def propose_booking(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    organization = await session.get(Organization, context.org_id)
    service_id = uuid.UUID(str(arguments["service_id"]))
    practitioner_id = uuid.UUID(str(arguments["practitioner_id"]))
    starts_at = _parse_start(arguments["starts_at"], organization)
    try:
        service, practitioner, start, _, _ = await validate_slot(
            session, context, service_id, practitioner_id, starts_at
        )
    except BookingRuleError as exc:
        return await _slot_refusal(session, context, exc, service_id, practitioner_id, starts_at)
    return await _propose(
        session,
        context,
        conversation_id,
        "create_booking",
        {
            "service_id": str(service_id),
            "practitioner_id": str(practitioner_id),
            "starts_at": start.isoformat(),
            "expected_price": str(service.price),
        },
        {
            "title": "Confirm your booking",
            "lines": [
                {"label": "Service", "value": f"{service.name} ({service.duration_minutes} min)"},
                {"label": "Practitioner", "value": practitioner.name},
                {"label": "When", "value": _when(start, organization)},
            ],
            "total": f"Rs {service.price}",
            "note": f"Times are in {organization.timezone}.",
        },
    )


async def _owned_booking(
    session: AsyncSession, context: RequestContext, booking_id: uuid.UUID
) -> Booking:
    booking = await session.scalar(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.org_id == context.org_id,
            Booking.customer_id == context.user_id,
        )
    )
    if booking is None:
        raise BookingRuleError("Booking not found", 404)
    return booking


def _fee_note(fee: Decimal, change: str) -> str:
    if fee > 0:
        return (
            f"This is within 24 hours of the appointment, so the late {change} fee of Rs {fee} "
            "(50%) applies, payable at your next visit."
        )
    return f"This is more than 24 hours ahead, so {change} is free."


async def propose_reschedule(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    organization = await session.get(Organization, context.org_id)
    booking_id = uuid.UUID(str(arguments["booking_id"]))
    starts_at = _parse_start(arguments["starts_at"], organization)
    try:
        booking = await _owned_booking(session, context, booking_id)
        if booking.status is not BookingStatus.CONFIRMED:
            raise BookingRuleError(f"A {booking.status.value} booking cannot be rescheduled")
        fee = late_fee_for(booking)
    except BookingRuleError as exc:
        return {"error": str(exc)}
    try:
        service, practitioner, start, _, _ = await validate_slot(
            session,
            context,
            booking.service_id,
            booking.practitioner_id,
            starts_at,
            exclude_booking_id=booking.id,
        )
    except BookingRuleError as exc:
        return await _slot_refusal(
            session,
            context,
            exc,
            booking.service_id,
            booking.practitioner_id,
            starts_at,
            booking.id,
        )
    return await _propose(
        session,
        context,
        conversation_id,
        "reschedule_booking",
        {
            "booking_id": str(booking.id),
            "starts_at": start.isoformat(),
            "expected_fee": str(fee),
        },
        {
            "title": "Confirm the new time",
            "lines": [
                {"label": "Service", "value": service.name},
                {"label": "Practitioner", "value": practitioner.name},
                {"label": "From", "value": _when(booking.starts_at, organization)},
                {"label": "To", "value": _when(start, organization)},
            ],
            "total": f"Rs {booking.price}",
            "note": _fee_note(fee, "rescheduling"),
        },
    )


async def propose_booking_cancel(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    organization = await session.get(Organization, context.org_id)
    try:
        booking = await _owned_booking(session, context, uuid.UUID(str(arguments["booking_id"])))
        if booking.status is not BookingStatus.CONFIRMED:
            raise BookingRuleError(f"A {booking.status.value} booking cannot be cancelled")
        fee = late_fee_for(booking)
    except BookingRuleError as exc:
        return {"error": str(exc)}
    service = await session.get(Service, booking.service_id)
    practitioner = await session.get(Practitioner, booking.practitioner_id)
    return await _propose(
        session,
        context,
        conversation_id,
        "cancel_booking",
        {"booking_id": str(booking.id), "expected_fee": str(fee)},
        {
            "title": "Cancel this booking?",
            "lines": [
                {"label": "Service", "value": service.name},
                {"label": "Practitioner", "value": practitioner.name},
                {"label": "When", "value": _when(booking.starts_at, organization)},
                {"label": "Cancellation fee", "value": f"Rs {fee}"},
            ],
            "note": _fee_note(fee, "cancellation"),
        },
    )


async def _check_fee_unchanged(
    session: AsyncSession, context: RequestContext, params: dict[str, Any]
) -> None:
    booking = await _owned_booking(session, context, uuid.UUID(params["booking_id"]))
    if late_fee_for(booking) != Decimal(params["expected_fee"]):
        raise BookingRuleError("The fee has changed; please review it again", 409)


async def _booking_result(
    session: AsyncSession, context: RequestContext, booking: Booking
) -> dict[str, Any]:
    organization = await session.get(Organization, context.org_id)
    service = await session.get(Service, booking.service_id)
    practitioner = await session.get(Practitioner, booking.practitioner_id)
    return booking_card(
        service.name,
        practitioner.name,
        _when(booking.starts_at, organization),
        booking.status.value,
        booking.late_fee or None,
    )


async def _order_result(
    session: AsyncSession, context: RequestContext, order_id: uuid.UUID
) -> dict[str, Any]:
    organization = await session.get(Organization, context.org_id)
    detail = await order_detail(session, context, order_id)
    return order_card(detail, organization.timezone)


async def _carry_out(
    session: AsyncSession, context: RequestContext, action: PendingAction
) -> tuple[str, dict[str, Any]]:
    """Run the service function; its commit also saves the action's confirmed status."""
    params = action.payload["params"]
    kind = action.action_type
    if kind == "create_order":
        order = await create_order(
            session,
            context,
            [RequestedItem(**item) for item in params["items"]],
            params["delivery_address"],
            Decimal(params["expected_total"]),
        )
        content = (
            f"Your order is placed. Order {order.id}, total Rs {order.total}, cash on delivery."
        )
        return content, await _order_result(session, context, order.id)
    if kind == "update_order":
        order = await update_pending_order(
            session,
            context,
            uuid.UUID(params["order_id"]),
            [RequestedItem(**item) for item in params["items"]],
            params["delivery_address"],
            Decimal(params["expected_total"]),
        )
        content = f"Order {order.id} is updated. The new total is Rs {order.total}."
        return content, await _order_result(session, context, order.id)
    if kind == "cancel_order":
        order = await cancel_pending_order(session, context, uuid.UUID(params["order_id"]))
        return f"Order {order.id} is cancelled.", await _order_result(session, context, order.id)
    if kind == "reorder":
        order = await reorder(
            session, context, uuid.UUID(params["order_id"]), Decimal(params["expected_total"])
        )
        content = f"Your new order is placed. Order {order.id}, total Rs {order.total}."
        return content, await _order_result(session, context, order.id)
    organization = await session.get(Organization, context.org_id)
    if kind == "create_booking":
        booking = await create_booking(
            session,
            context,
            uuid.UUID(params["service_id"]),
            uuid.UUID(params["practitioner_id"]),
            datetime.fromisoformat(params["starts_at"]),
            Decimal(params["expected_price"]),
        )
        content = f"You're booked for {_when(booking.starts_at, organization)}."
        return content, await _booking_result(session, context, booking)
    if kind == "reschedule_booking":
        await _check_fee_unchanged(session, context, params)
        booking = await reschedule_booking(
            session,
            context,
            uuid.UUID(params["booking_id"]),
            datetime.fromisoformat(params["starts_at"]),
        )
        content = f"Your booking is moved to {_when(booking.starts_at, organization)}."
        return content, await _booking_result(session, context, booking)
    if kind == "cancel_booking":
        await _check_fee_unchanged(session, context, params)
        booking = await cancel_booking(session, context, uuid.UUID(params["booking_id"]))
        content = f"Your booking is cancelled. Cancellation fee: Rs {booking.late_fee}."
        return content, await _booking_result(session, context, booking)
    raise ActionError(f"Unknown action {kind!r}", 400)


async def _open_action(
    session: AsyncSession,
    context: RequestContext,
    conversation: Conversation,
    action_id: uuid.UUID,
) -> PendingAction:
    """Lock the customer's own action, so a double click can't carry it out twice."""
    action = await session.scalar(
        select(PendingAction)
        .where(
            PendingAction.id == action_id,
            PendingAction.org_id == context.org_id,
            PendingAction.user_id == context.user_id,
            PendingAction.conversation_id == conversation.id,
        )
        .with_for_update()
    )
    if action is None:
        raise ActionError("Action not found", 404)
    if action.status is not PendingActionStatus.PENDING:
        raise ActionError(f"This request is already {action.status.value}", 409)
    expires_at = action.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        action.status = PendingActionStatus.EXPIRED
        await session.commit()
        raise ActionError("This request has expired; please ask again", 409)
    return action


def _reply(
    conversation: Conversation, content: str, cards: list[dict[str, Any]] | None = None
) -> ChatMessage:
    return ChatMessage(
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=content,
        citations=[],
        cards=cards or [],
    )


async def confirm_action(
    session: AsyncSession,
    context: RequestContext,
    conversation: Conversation,
    action_id: uuid.UUID,
) -> tuple[ChatMessage, PendingAction]:
    action = await _open_action(session, context, conversation, action_id)
    action.status = PendingActionStatus.CONFIRMED
    action.confirmed_at = datetime.now(UTC)
    cards: list[dict[str, Any]] = []
    try:
        content, card = await _carry_out(session, context, action)
        cards.append(card)
    except (OrderRuleError, BookingRuleError) as exc:
        await session.rollback()
        await session.refresh(action)
        await session.refresh(conversation)
        action.status = PendingActionStatus.CANCELLED
        content = f"I couldn't do that, and nothing was changed: {exc}."
    reply = _reply(conversation, content, cards)
    session.add(reply)
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return reply, action


async def dismiss_action(
    session: AsyncSession,
    context: RequestContext,
    conversation: Conversation,
    action_id: uuid.UUID,
) -> tuple[ChatMessage, PendingAction]:
    action = await _open_action(session, context, conversation, action_id)
    action.status = PendingActionStatus.CANCELLED
    reply = _reply(conversation, "No problem, I haven't changed anything.")
    session.add(reply)
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return reply, action


async def choose_slot(
    session: AsyncSession,
    context: RequestContext,
    conversation: Conversation,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID,
    starts_at: str,
    booking_id: uuid.UUID | None = None,
) -> tuple[ChatMessage, ChatMessage]:
    """A slot button goes straight to the normal proposal, with every rule checked and no LLM."""
    organization = await session.get(Organization, context.org_id)
    conversation_id = conversation.id
    arguments = {"starts_at": starts_at}
    try:
        label = _when(_parse_start(starts_at, organization), organization)
        if booking_id:
            result = await propose_reschedule(
                session, context, conversation_id, {**arguments, "booking_id": str(booking_id)}
            )
        else:
            result = await propose_booking(
                session,
                context,
                conversation_id,
                {
                    **arguments,
                    "service_id": str(service_id),
                    "practitioner_id": str(practitioner_id),
                },
            )
    except BookingRuleError as exc:
        label, result = starts_at, {"error": str(exc)}
    question = ChatMessage(
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=label,
        citations=[],
    )
    session.add(question)
    await session.flush()
    card = result.pop("card", None)
    if "error" in result:
        content = f"{result['error']}. Please choose another time."
    else:
        content = "Please check the details and press Confirm."
    reply = _reply(conversation, content, [card] if card else [])
    session.add(reply)
    await session.flush()
    if result.get("action_id"):
        await session.execute(
            update(PendingAction)
            .where(PendingAction.id == uuid.UUID(result["action_id"]))
            .values(message_id=reply.id)
        )
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    return question, reply


def action_response(action: PendingAction) -> dict[str, Any]:
    expires_at = action.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    expired = action.status is PendingActionStatus.PENDING and expires_at <= datetime.now(UTC)
    return {
        "id": str(action.id),
        "type": action.action_type,
        "status": PendingActionStatus.EXPIRED.value if expired else action.status.value,
        "summary": action.payload["summary"],
    }


async def actions_by_message(
    session: AsyncSession, conversation_id: uuid.UUID
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    rows = await session.scalars(
        select(PendingAction)
        .where(
            PendingAction.conversation_id == conversation_id,
            PendingAction.message_id.is_not(None),
        )
        .order_by(PendingAction.created_at)
    )
    grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for action in rows:
        grouped.setdefault(action.message_id, []).append(action_response(action))
    return grouped
