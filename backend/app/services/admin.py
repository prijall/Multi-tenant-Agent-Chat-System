"""Read-only, tenant-scoped operational analytics for administrators."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RequestContext
from app.db.models import Booking, Order, OrderStatus, Practitioner, User


async def analytics(
    session: AsyncSession,
    context: RequestContext,
    starts_at: datetime,
    ends_at: datetime,
) -> dict[str, object]:
    order_rows = await session.execute(
        select(Order.status, func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(
            Order.org_id == context.org_id,
            Order.created_at >= starts_at,
            Order.created_at < ends_at,
        )
        .group_by(Order.status)
    )
    booking_rows = await session.execute(
        select(Booking.status, func.count(Booking.id))
        .where(
            Booking.org_id == context.org_id,
            Booking.created_at >= starts_at,
            Booking.created_at < ends_at,
        )
        .group_by(Booking.status)
    )
    practitioner_rows = await session.execute(
        select(Practitioner.name, func.count(Booking.id))
        .join(Booking, Booking.practitioner_id == Practitioner.id)
        .where(
            Booking.org_id == context.org_id,
            Booking.starts_at >= starts_at,
            Booking.starts_at < ends_at,
        )
        .group_by(Practitioner.id, Practitioner.name)
        .order_by(func.count(Booking.id).desc())
    )
    new_customers = await session.scalar(
        select(func.count(User.id)).where(
            User.org_id == context.org_id,
            User.created_at >= starts_at,
            User.created_at < ends_at,
        )
    )
    orders = [
        {"status": status.value, "count": count, "value": str(Decimal(value))}
        for status, count, value in order_rows
    ]
    # A cancelled order earned nothing, though it still appears in the counts by status.
    counted = [item for item in orders if item["status"] != OrderStatus.CANCELLED.value]
    order_count = sum(item["count"] for item in counted)
    order_value = sum((Decimal(item["value"]) for item in counted), Decimal("0"))
    return {
        "window": {"from": starts_at.isoformat(), "to": ends_at.isoformat()},
        "definitions": {
            "order_value": "Sum of order totals created in the window, excluding cancelled "
            "orders and delivery charges.",
            "average_order_value": "Order value divided by non-cancelled orders in the window.",
            "new_customers": "Customer and admin profiles created in the selected window.",
            "practitioner_activity": "Appointments starting in the selected window.",
        },
        "orders": orders,
        "order_value": str(order_value),
        "average_order_value": str(order_value / order_count if order_count else Decimal("0")),
        "bookings": [{"status": status.value, "count": count} for status, count in booking_rows],
        "practitioners": [
            {"name": name, "booking_count": count} for name, count in practitioner_rows
        ],
        "new_customers": new_customers or 0,
    }
