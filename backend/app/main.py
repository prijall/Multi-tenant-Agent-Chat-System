import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.actions import (
    ActionError,
    action_response,
    actions_by_message,
    choose_slot,
    confirm_action,
    dismiss_action,
)
from app.agents.contracts import AgentProvider
from app.agents.providers import build_provider, build_suggestion_provider
from app.agents.runtime import owned_conversation, stream_agent
from app.agents.suggestions import suggest_next_message
from app.core.config import get_settings
from app.core.rate_limit import interactive_limiter, suggestion_limiter
from app.core.security import (
    RequestContext,
    authenticated_claims,
    authenticated_subject,
    require_admin,
    resolve_context_from_session,
)
from app.db.database import get_session, get_session_factory
from app.db.models import (
    Booking,
    ChatMessage,
    Conversation,
    Order,
    OrderStatus,
    OrderStatusEvent,
    Organization,
    PendingAction,
    User,
    UserRole,
)
from app.db.repositories import list_customer_orders, list_products
from app.services.admin import analytics
from app.services.bookings import (
    BookingRuleError,
    available_slots,
    cancel_booking,
    create_booking,
    customer_bookings,
    list_practitioners,
    list_services,
    nearest_slots,
    reschedule_booking,
    validate_slot,
)
from app.services.knowledge import search_knowledge
from app.services.orders import (
    OrderRuleError,
    RequestedItem,
    cancel_pending_order,
    create_order,
    order_detail,
    quote_order,
    reorder,
    update_pending_order,
)

logger = logging.getLogger(__name__)

NEXT_ORDER_STATUS = {
    OrderStatus.PENDING: OrderStatus.CONFIRMED,
    OrderStatus.CONFIRMED: OrderStatus.SHIPPED,
    OrderStatus.SHIPPED: OrderStatus.DELIVERED,
}

app = FastAPI(title="Multi-Tenant Agent Platform", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderItemInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=100)


class OrderQuoteInput(BaseModel):
    items: list[OrderItemInput] = Field(min_length=1, max_length=50)


class OrderConfirmInput(OrderQuoteInput):
    delivery_address: str = Field(min_length=3, max_length=500)
    expected_total: Decimal = Field(ge=0)


class ReorderInput(BaseModel):
    expected_total: Decimal = Field(ge=0)


class BookingQuoteInput(BaseModel):
    service_id: uuid.UUID
    practitioner_id: uuid.UUID
    starts_at: datetime


class BookingConfirmInput(BookingQuoteInput):
    expected_price: Decimal = Field(ge=0)


class RescheduleInput(BaseModel):
    starts_at: datetime


class OrderStatusInput(BaseModel):
    status: OrderStatus


class MembershipInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: UserRole = UserRole.CUSTOMER


class MembershipRoleInput(BaseModel):
    role: UserRole


class ConversationInput(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class SlotChoiceInput(BaseModel):
    service_id: uuid.UUID
    practitioner_id: uuid.UUID
    starts_at: str = Field(min_length=10, max_length=40)
    booking_id: uuid.UUID | None = None


class ChatInput(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


def requested_items(payload: OrderQuoteInput) -> list[RequestedItem]:
    return [RequestedItem(item.sku, item.quantity) for item in payload.items]


def order_http_error(exc: OrderRuleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def booking_http_error(exc: BookingRuleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def get_request_context(
    auth_subject: str = Depends(authenticated_subject),
    session: AsyncSession = Depends(get_session),
) -> RequestContext:
    return await resolve_context_from_session(session, auth_subject)


async def rate_limited_context(
    context: RequestContext = Depends(get_request_context),
) -> RequestContext:
    interactive_limiter.check(str(context.user_id))
    return context


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/database")
async def database_health(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    value = await session.scalar(text("SELECT 1"))
    if value != 1:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok", "database": "connected"}


@app.post("/api/provision/{organization_slug}")
async def provision_customer_profile(
    organization_slug: str,
    claims: Annotated[dict[str, Any], Depends(authenticated_claims)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Provision signup into the organization fixed by its branded entry point."""
    organization = await session.scalar(
        select(Organization).where(Organization.slug == organization_slug)
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    subject = str(claims["sub"])
    existing_identity = await session.scalar(
        select(User).where(User.auth_subject == subject)
    )
    if existing_identity is not None:
        if existing_identity.org_id != organization.id:
            raise HTTPException(
                status_code=409,
                detail="Identity belongs to another organization",
            )
        return {
            "status": "ready",
            "organization": organization.slug,
            "role": existing_identity.role,
        }

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="The identity has no email address")

    existing_email = await session.scalar(select(User).where(User.email == email))
    if existing_email is not None:
        if existing_email.org_id != organization.id:
            raise HTTPException(status_code=409, detail="Email belongs to another organization")
        if existing_email.auth_subject is not None:
            raise HTTPException(status_code=409, detail="Email is already registered")
        # A pending profile can carry a role and history, so taking it over needs proof of email.
        verified = claims.get("email_verified", claims.get("emailVerified"))
        if verified is not True:
            raise HTTPException(
                status_code=403, detail="Verify your email before activating access"
            )
        existing_email.auth_subject = subject
        await session.commit()
        return {
            "status": "linked",
            "organization": organization.slug,
            "role": existing_email.role,
        }

    user = User(
        org_id=organization.id,
        auth_subject=subject,
        email=email,
        role=UserRole.CUSTOMER,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from exc
    return {"status": "created", "organization": organization.slug, "role": user.role}


@app.get("/api/me")
async def me(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    organization = await session.get(Organization, context.org_id)
    return {"organization": organization.slug, "role": context.role}


def conversation_response(conversation: Conversation) -> dict[str, object]:
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def message_response(
    message: ChatMessage, actions: list[dict[str, Any]] | None = None
) -> dict[str, object]:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "citations": message.citations,
        "cards": message.cards or [],
        "created_at": message.created_at,
        "actions": actions or [],
    }


@app.post("/api/conversations", status_code=201)
async def create_conversation(
    payload: ConversationInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = Conversation(
        org_id=context.org_id,
        user_id=context.user_id,
        title=payload.title.strip(),
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation_response(conversation)


@app.get("/api/conversations")
async def conversations(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await session.scalars(
        select(Conversation)
        .where(
            Conversation.org_id == context.org_id,
            Conversation.user_id == context.user_id,
        )
        .order_by(Conversation.updated_at.desc())
    )
    return [conversation_response(row) for row in rows]


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    conversation.title = payload.title.strip()
    await session.commit()
    return conversation_response(conversation)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    conversation = await owned_conversation(session, context, conversation_id)
    await session.delete(conversation)
    await session.commit()
    return {"status": "deleted"}


@app.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    conversation = await owned_conversation(session, context, conversation_id)
    rows = await session.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    actions = await actions_by_message(session, conversation.id)
    return [message_response(row, actions.get(row.id)) for row in rows]


async def resolve_action(
    resolver: Callable[..., Awaitable[tuple[ChatMessage, PendingAction]]],
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    try:
        message, action = await resolver(session, context, conversation, action_id)
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"message": message_response(message), "action": action_response(action)}


@app.post("/api/conversations/{conversation_id}/actions/{action_id}/confirm")
async def confirm_conversation_action(
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """The only way a chat proposal takes effect: the customer's own request, never the model's."""
    return await resolve_action(confirm_action, session, context, conversation_id, action_id)


@app.post("/api/conversations/{conversation_id}/actions/{action_id}/dismiss")
async def dismiss_conversation_action(
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    return await resolve_action(dismiss_action, session, context, conversation_id, action_id)


@app.post("/api/conversations/{conversation_id}/slot-choice")
async def choose_conversation_slot(
    conversation_id: uuid.UUID,
    payload: SlotChoiceInput,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    question, reply = await choose_slot(
        session,
        context,
        conversation,
        payload.service_id,
        payload.practitioner_id,
        payload.starts_at,
        payload.booking_id,
    )
    actions = await actions_by_message(session, conversation_id)
    return {
        "question": message_response(question),
        "message": message_response(reply, actions.get(reply.id)),
    }


def get_agent_provider() -> AgentProvider:
    provider = build_provider(get_settings())
    if provider is None:
        raise HTTPException(status_code=503, detail="The agent's LLM is not configured")
    return provider


def server_sent_event(event: dict[str, object]) -> str:
    return f"data: {json.dumps(jsonable_encoder(event))}\n\n"


async def agent_events(
    session_factory: async_sessionmaker[AsyncSession],
    context: RequestContext,
    conversation_id: uuid.UUID,
    content: str,
    provider: AgentProvider,
) -> AsyncIterator[str]:
    """Runs on its own session: a request-scoped one may close before the stream finishes."""
    async with session_factory() as session:
        conversation = await owned_conversation(session, context, conversation_id)
        try:
            async with aclosing(
                stream_agent(
                    session,
                    context,
                    conversation,
                    content,
                    provider,
                    max_tool_rounds=get_settings().agent_max_tool_rounds,
                )
            ) as events:
                async for event in events:
                    if event["type"] == "done":
                        message = event["message"]
                        actions = await actions_by_message(session, conversation_id)
                        event = {
                            "type": "done",
                            "message": message_response(message, actions.get(message.id)),
                        }
                    yield server_sent_event(event)
        except Exception:
            logger.exception("Agent run failed for conversation %s", conversation_id)
            yield server_sent_event(
                {"type": "error", "detail": "The agent could not complete the request"}
            )


def get_suggestion_provider() -> AgentProvider:
    provider = build_suggestion_provider(get_settings())
    if provider is None:
        raise HTTPException(status_code=503, detail="The agent's LLM is not configured")
    return provider


@app.post("/api/conversations/{conversation_id}/suggestion")
async def next_message_suggestion(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
    provider: AgentProvider = Depends(get_suggestion_provider),
) -> dict[str, str | None]:
    conversation = await owned_conversation(session, context, conversation_id)
    suggestion_limiter.check(str(context.user_id))
    return {"suggestion": await suggest_next_message(session, conversation, provider)}


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    payload: ChatInput,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    provider: AgentProvider = Depends(get_agent_provider),
) -> StreamingResponse:
    await owned_conversation(session, context, conversation_id)
    await session.rollback()
    return StreamingResponse(
        agent_events(session_factory, context, conversation_id, payload.content, provider),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def membership_response(user: User) -> dict[str, str]:
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "status": "active" if user.auth_subject else "pending",
    }


@app.get("/api/admin/users")
async def admin_users(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, str]]:
    require_admin(context)
    users = await session.scalars(
        select(User).where(User.org_id == context.org_id).order_by(User.email)
    )
    return [membership_response(user) for user in users]


@app.post("/api/admin/users", status_code=201)
async def create_admin_user(
    payload: MembershipInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    require_admin(context)
    email = payload.email.strip().lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email is already assigned")
    user = User(org_id=context.org_id, email=email, role=payload.role)
    session.add(user)
    await session.commit()
    return membership_response(user)


@app.patch("/api/admin/users/{user_id}")
async def update_admin_user(
    user_id: uuid.UUID,
    payload: MembershipRoleInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    require_admin(context)
    user = await session.scalar(
        select(User).where(User.id == user_id, User.org_id == context.org_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == context.user_id and payload.role is not UserRole.ADMIN:
        raise HTTPException(status_code=409, detail="You cannot remove your own admin access")
    user.role = payload.role
    await session.commit()
    return membership_response(user)


@app.delete("/api/admin/users/{user_id}")
async def delete_admin_user(
    user_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    require_admin(context)
    user = await session.scalar(
        select(User).where(User.id == user_id, User.org_id == context.org_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == context.user_id:
        raise HTTPException(status_code=409, detail="You cannot remove your own access")
    await session.delete(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="User has related records and cannot be removed",
        ) from exc
    return {"status": "deleted"}


@app.get("/api/knowledge/search")
async def knowledge_search(
    q: str,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    hits = await search_knowledge(session, context, q)
    return [asdict(hit) for hit in hits]


@app.get("/api/products")
async def products(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await list_products(session, context)
    return [
        {"sku": row.sku, "name": row.name, "price": str(row.price), "stock": row.stock}
        for row in rows
    ]


@app.get("/api/orders")
async def orders(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await list_customer_orders(session, context)
    return [
        {
            "id": row.id,
            "status": row.status,
            "total": str(row.total),
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.get("/api/orders/{order_id}")
async def order(
    order_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        return await order_detail(session, context, order_id)
    except OrderRuleError as exc:
        raise order_http_error(exc) from exc


@app.post("/api/order-quotes")
async def order_quote(
    payload: OrderQuoteInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        lines, total, _ = await quote_order(session, context, requested_items(payload))
        return {"items": lines, "total": str(total)}
    except OrderRuleError as exc:
        raise order_http_error(exc) from exc


@app.post("/api/orders", status_code=201)
async def confirm_order(
    payload: OrderConfirmInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        created = await create_order(
            session,
            context,
            requested_items(payload),
            payload.delivery_address,
            payload.expected_total,
        )
        return await order_detail(session, context, created.id)
    except OrderRuleError as exc:
        await session.rollback()
        raise order_http_error(exc) from exc


@app.put("/api/orders/{order_id}")
async def update_order(
    order_id: uuid.UUID,
    payload: OrderConfirmInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        updated = await update_pending_order(
            session,
            context,
            order_id,
            requested_items(payload),
            payload.delivery_address,
            payload.expected_total,
        )
        return await order_detail(session, context, updated.id)
    except OrderRuleError as exc:
        await session.rollback()
        raise order_http_error(exc) from exc


@app.delete("/api/orders/{order_id}")
async def cancel_order(
    order_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        cancelled = await cancel_pending_order(session, context, order_id)
        return await order_detail(session, context, cancelled.id)
    except OrderRuleError as exc:
        await session.rollback()
        raise order_http_error(exc) from exc


@app.post("/api/orders/{order_id}/reorder", status_code=201)
async def reorder_previous_order(
    order_id: uuid.UUID,
    payload: ReorderInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        created = await reorder(session, context, order_id, payload.expected_total)
        return await order_detail(session, context, created.id)
    except OrderRuleError as exc:
        await session.rollback()
        raise order_http_error(exc) from exc


@app.get("/api/orders/{order_id}/reorder-quote")
async def quote_previous_order(
    order_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        previous = await order_detail(session, context, order_id)
        items = [
            RequestedItem(str(item["sku"]), int(item["quantity"]))
            for item in previous["items"]
        ]
        lines, total, _ = await quote_order(session, context, items)
        return {
            "items": lines,
            "total": str(total),
            "delivery_address": previous["delivery_address"],
        }
    except OrderRuleError as exc:
        raise order_http_error(exc) from exc


@app.get("/api/admin/orders")
async def admin_orders(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    require_admin(context)
    rows = await session.execute(
        select(Order, User.email)
        .join(User, User.id == Order.customer_id)
        .where(Order.org_id == context.org_id)
        .order_by(Order.created_at.desc())
        .limit(30)
    )
    return [
        {
            "id": str(order.id),
            "customer": email,
            "status": order.status.value,
            "total": str(order.total),
            "created_at": order.created_at,
            "next_status": next_status.value if next_status else None,
        }
        for order, email in rows
        for next_status in [NEXT_ORDER_STATUS.get(order.status)]
    ]


@app.patch("/api/admin/orders/{order_id}/status")
async def advance_order_status(
    order_id: uuid.UUID,
    payload: OrderStatusInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    require_admin(context)
    order = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.org_id == context.org_id)
        .with_for_update()
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if NEXT_ORDER_STATUS.get(order.status) is not payload.status:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move an order from {order.status.value} to {payload.status.value}",
        )
    order.status = payload.status
    session.add(OrderStatusEvent(order_id=order.id, status=payload.status))
    await session.commit()
    return {"id": str(order.id), "status": order.status.value}


@app.get("/api/admin/analytics")
async def admin_analytics(
    date_from: datetime,
    date_to: datetime,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    require_admin(context)
    if date_to <= date_from:
        raise HTTPException(status_code=422, detail="date_to must be after date_from")
    return await analytics(session, context, date_from, date_to)


@app.get("/api/services")
async def services(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await list_services(session, context)
    return [
        {
            "id": str(row.id),
            "code": row.code,
            "name": row.name,
            "category": row.category,
            "duration_minutes": row.duration_minutes,
            "price": str(row.price),
        }
        for row in rows
    ]


@app.get("/api/practitioners")
async def practitioners(
    service_id: uuid.UUID | None = None,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, str]]:
    rows = await list_practitioners(session, context, service_id)
    return [{"id": str(row.id), "code": row.code, "name": row.name} for row in rows]


@app.get("/api/availability")
async def availability(
    service_id: uuid.UUID,
    date_from: date,
    date_to: date,
    practitioner_id: uuid.UUID | None = None,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    try:
        return await available_slots(
            session, context, service_id, date_from, date_to, practitioner_id
        )
    except BookingRuleError as exc:
        raise booking_http_error(exc) from exc


@app.post("/api/booking-quotes")
async def booking_quote(
    payload: BookingQuoteInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        service, practitioner, start, end, organization = await validate_slot(
            session,
            context,
            payload.service_id,
            payload.practitioner_id,
            payload.starts_at,
        )
        return {
            "service_id": str(service.id),
            "service": service.name,
            "practitioner_id": str(practitioner.id),
            "practitioner": practitioner.name,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "timezone": organization.timezone,
            "duration_minutes": service.duration_minutes,
            "price": str(service.price),
        }
    except BookingRuleError as exc:
        raise booking_http_error(exc) from exc


@app.get("/api/bookings")
async def bookings(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    return await customer_bookings(session, context)


@app.post("/api/bookings", status_code=201)
async def confirm_booking(
    payload: BookingConfirmInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    try:
        booking = await create_booking(
            session,
            context,
            payload.service_id,
            payload.practitioner_id,
            payload.starts_at,
            payload.expected_price,
        )
        return {"id": str(booking.id), "status": booking.status.value}
    except BookingRuleError as exc:
        await session.rollback()
        raise booking_http_error(exc) from exc


@app.delete("/api/bookings/{booking_id}")
async def cancel_customer_booking(
    booking_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    try:
        booking = await cancel_booking(session, context, booking_id)
        return {"id": str(booking.id), "status": booking.status.value}
    except BookingRuleError as exc:
        await session.rollback()
        raise booking_http_error(exc) from exc


@app.put("/api/bookings/{booking_id}")
async def reschedule_customer_booking(
    booking_id: uuid.UUID,
    payload: RescheduleInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        booking = await reschedule_booking(
            session, context, booking_id, payload.starts_at
        )
        return {
            "id": str(booking.id),
            "status": booking.status.value,
            "starts_at": booking.starts_at.isoformat(),
        }
    except BookingRuleError as exc:
        await session.rollback()
        alternatives: list[dict[str, object]] = []
        if exc.status_code == 409:
            booking = await session.get(Booking, booking_id)
            if (
                booking
                and booking.org_id == context.org_id
                and booking.customer_id == context.user_id
            ):
                alternatives = await nearest_slots(
                    session,
                    context,
                    booking.service_id,
                    booking.practitioner_id,
                    payload.starts_at,
                    exclude_booking_id=booking.id,
                )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), "alternatives": alternatives},
        ) from exc

