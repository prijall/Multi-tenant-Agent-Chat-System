import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import actions
from app.agents.cards import bookings_card, order_card, orders_card, slots_card
from app.core.security import RequestContext
from app.db.models import Holiday, OpeningHours, Order, Organization, Product
from app.db.repositories import list_customer_orders
from app.services.bookings import available_slots, customer_bookings, list_services
from app.services.knowledge import search_knowledge
from app.services.orders import order_detail

RECENT_ORDERS = 10

ORDER_ID = {"type": "string", "description": "The order's id, from list_my_orders"}
BOOKING_ID = {"type": "string", "description": "The booking's id, from list_my_bookings"}
ORDER_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "sku": {"type": "string", "description": "The exact SKU from search_products"},
            "quantity": {"type": "integer"},
        },
        "required": ["sku", "quantity"],
    },
}
STARTS_AT = {
    "type": "string",
    "description": "The slot's local_start value, an ISO 8601 time with its UTC offset",
}

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def declaration(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _json(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, date, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _local_times_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The model repeats whatever time it is given, so it only ever sees studio-local ones."""
    return [
        {key: value for key, value in row.items() if key not in {"starts_at", "ends_at"}}
        for row in rows
    ]


class TenantToolRegistry:
    def __init__(self, tools: list[ToolDefinition]):
        self._tools = {tool.name: tool for tool in tools}

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def declarations(self) -> list[dict[str, Any]]:
        return [tool.declaration() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        tool = self._tools.get(name)
        if tool is None:
            raise PermissionError(f"Tool {name!r} is not available to this organization")
        started = monotonic()
        result = await tool.handler(arguments)
        return _json(result), round((monotonic() - started) * 1000)


async def registry_for(
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID | None = None,
) -> tuple[str, TenantToolRegistry]:
    organization = await session.get(Organization, context.org_id)
    if organization is None:
        raise RuntimeError("Authenticated organization no longer exists")
    timezone = organization.timezone
    slug = organization.slug

    def proposing(propose: Callable[..., Awaitable[dict[str, Any]]]) -> ToolHandler:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            if conversation_id is None:
                raise RuntimeError("Write tools need a conversation")
            return await propose(session, context, conversation_id, arguments)

        return handler

    async def knowledge(arguments: dict[str, Any]) -> dict[str, Any]:
        hits = await search_knowledge(session, context, str(arguments.get("query", "")))
        if not hits:
            return {"results": [], "note": "Nothing relevant found. Say you don't know."}
        return {
            "results": [
                {
                    "document": hit.document,
                    "section": hit.section,
                    "content": hit.content,
                    "score": hit.score,
                }
                for hit in hits
            ]
        }

    common = ToolDefinition(
        "search_knowledge",
        "Search this organization's policies, FAQs, descriptions and guides.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        knowledge,
    )

    if slug == "himali-tea":
        async def products(arguments: dict[str, Any]) -> dict[str, Any]:
            statement = select(Product).where(Product.org_id == context.org_id)
            query = str(arguments.get("query") or "").strip()
            category = str(arguments.get("category") or "").strip()
            if query:
                statement = statement.where(Product.name.ilike(f"%{query}%"))
            if category:
                statement = statement.where(Product.category.ilike(category))
            if arguments.get("min_price") is not None:
                statement = statement.where(Product.price >= Decimal(str(arguments["min_price"])))
            if arguments.get("max_price") is not None:
                statement = statement.where(Product.price <= Decimal(str(arguments["max_price"])))
            rows = await session.scalars(statement.order_by(Product.name).limit(25))
            return {
                "products": [
                    {
                        "sku": row.sku,
                        "name": row.name,
                        "category": row.category,
                        "price": str(row.price),
                        "stock": row.stock,
                    }
                    for row in rows
                ]
            }

        async def orders(_: dict[str, Any]) -> dict[str, Any]:
            rows: list[Order] = list(await list_customer_orders(session, context))
            details = [
                await order_detail(session, context, row.id) for row in rows[:RECENT_ORDERS]
            ]
            result: dict[str, Any] = {"orders": details}
            if details:
                result["card"] = orders_card(details, timezone)
            return result

        async def order_status(arguments: dict[str, Any]) -> dict[str, Any]:
            detail = await order_detail(session, context, uuid.UUID(str(arguments["order_id"])))
            return {**detail, "card": order_card(detail, timezone)}

        return slug, TenantToolRegistry(
            [
                common,
                ToolDefinition(
                    "search_products",
                    "Find products using live name, category, price and stock data.",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "category": {"type": "string"},
                            "min_price": {"type": "number"},
                            "max_price": {"type": "number"},
                        },
                    },
                    products,
                ),
                ToolDefinition(
                    "list_my_orders",
                    "List the signed-in customer's recent orders with items, newest first.",
                    {"type": "object", "properties": {}},
                    orders,
                ),
                ToolDefinition(
                    "get_order_status",
                    "Track one order: status, items, total and status history.",
                    {
                        "type": "object",
                        "properties": {"order_id": ORDER_ID},
                        "required": ["order_id"],
                    },
                    order_status,
                ),
                ToolDefinition(
                    "create_order",
                    "Propose a new order. The customer sees a summary and must press Confirm.",
                    {
                        "type": "object",
                        "properties": {
                            "items": ORDER_ITEMS,
                            "delivery_address": {"type": "string"},
                        },
                        "required": ["items", "delivery_address"],
                    },
                    proposing(actions.propose_order),
                ),
                ToolDefinition(
                    "update_order",
                    "Propose changing a pending order's items or address. Pass the full new "
                    "item list. The customer must press Confirm.",
                    {
                        "type": "object",
                        "properties": {
                            "order_id": ORDER_ID,
                            "items": ORDER_ITEMS,
                            "delivery_address": {"type": "string"},
                        },
                        "required": ["order_id"],
                    },
                    proposing(actions.propose_order_update),
                ),
                ToolDefinition(
                    "cancel_order",
                    "Propose cancelling a pending order. The customer must press Confirm.",
                    {
                        "type": "object",
                        "properties": {"order_id": ORDER_ID},
                        "required": ["order_id"],
                    },
                    proposing(actions.propose_order_cancel),
                ),
                ToolDefinition(
                    "reorder",
                    "Propose repeating a previous order at current prices, noting any price "
                    "changes. The customer must press Confirm.",
                    {
                        "type": "object",
                        "properties": {"order_id": ORDER_ID},
                        "required": ["order_id"],
                    },
                    proposing(actions.propose_reorder),
                ),
            ]
        )

    if slug == "lotus-wellness":
        async def services(_: dict[str, Any]) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "category": row.category,
                        "duration_minutes": row.duration_minutes,
                        "price": str(row.price),
                    }
                    for row in await list_services(session, context)
                ]
            }

        async def hours(_: dict[str, Any]) -> dict[str, Any]:
            today = datetime.now(ZoneInfo(timezone)).date()
            opening = await session.scalars(
                select(OpeningHours)
                .where(OpeningHours.org_id == context.org_id)
                .order_by(OpeningHours.weekday)
            )
            holidays = await session.scalars(
                select(Holiday)
                .where(Holiday.org_id == context.org_id, Holiday.day >= today)
                .order_by(Holiday.day)
                .limit(20)
            )
            return {
                "timezone": timezone,
                "weekly_hours": [
                    {
                        "weekday": row.weekday,
                        "opens_at": str(row.opens_at),
                        "closes_at": str(row.closes_at),
                    }
                    for row in opening
                ],
                "holidays": [{"day": str(row.day), "name": row.name} for row in holidays],
            }

        async def bookings(_: dict[str, Any]) -> dict[str, Any]:
            rows = await customer_bookings(session, context)
            result: dict[str, Any] = {"bookings": _local_times_only(rows)}
            if rows:
                result["card"] = bookings_card(rows, timezone)
            return result

        async def slots(arguments: dict[str, Any]) -> dict[str, Any]:
            service_id = uuid.UUID(str(arguments["service_id"]))
            booking_id = str(arguments.get("booking_id") or "") or None
            rows = await available_slots(
                session,
                context,
                service_id,
                date.fromisoformat(str(arguments["date_from"])),
                date.fromisoformat(str(arguments["date_to"])),
                uuid.UUID(str(arguments["practitioner_id"]))
                if arguments.get("practitioner_id")
                else None,
                exclude_booking_id=uuid.UUID(booking_id) if booking_id else None,
            )
            result: dict[str, Any] = {"slots": _local_times_only(rows)}
            if rows:
                result["card"] = slots_card(rows, str(service_id), booking_id)
            return result

        return slug, TenantToolRegistry(
            [
                common,
                ToolDefinition(
                    "list_services",
                    "List live services with duration and price.",
                    {"type": "object", "properties": {}},
                    services,
                ),
                ToolDefinition(
                    "get_opening_hours",
                    "Return weekly opening hours, holidays and the studio timezone.",
                    {"type": "object", "properties": {}},
                    hours,
                ),
                ToolDefinition(
                    "find_available_slots",
                    "Find free appointment slots for a service and date range.",
                    {
                        "type": "object",
                        "properties": {
                            "service_id": {"type": "string"},
                            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                            "practitioner_id": {"type": "string"},
                            "booking_id": BOOKING_ID,
                        },
                        "required": ["service_id", "date_from", "date_to"],
                    },
                    slots,
                ),
                ToolDefinition(
                    "list_my_bookings",
                    "List the signed-in customer's upcoming and past bookings.",
                    {"type": "object", "properties": {}},
                    bookings,
                ),
                ToolDefinition(
                    "create_booking",
                    "Propose booking a free slot. The customer sees a summary and must press "
                    "Confirm.",
                    {
                        "type": "object",
                        "properties": {
                            "service_id": {"type": "string"},
                            "practitioner_id": {"type": "string"},
                            "starts_at": STARTS_AT,
                        },
                        "required": ["service_id", "practitioner_id", "starts_at"],
                    },
                    proposing(actions.propose_booking),
                ),
                ToolDefinition(
                    "reschedule_booking",
                    "Propose moving a booking. If the time conflicts, this returns the nearest "
                    "free slots to offer. The customer must press Confirm.",
                    {
                        "type": "object",
                        "properties": {"booking_id": BOOKING_ID, "starts_at": STARTS_AT},
                        "required": ["booking_id", "starts_at"],
                    },
                    proposing(actions.propose_reschedule),
                ),
                ToolDefinition(
                    "cancel_booking",
                    "Propose cancelling a booking, showing any fee under the cancellation "
                    "policy. The customer must press Confirm.",
                    {
                        "type": "object",
                        "properties": {"booking_id": BOOKING_ID},
                        "required": ["booking_id"],
                    },
                    proposing(actions.propose_booking_cancel),
                ),
            ]
        )

    raise RuntimeError(f"No agent is configured for organization {slug!r}")
