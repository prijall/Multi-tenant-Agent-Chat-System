import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_context
from app.api.schemas import MembershipInput, MembershipRoleInput, OrderStatusInput
from app.core.security import RequestContext, require_admin
from app.db.database import get_session
from app.db.models import Order, OrderStatus, OrderStatusEvent, User, UserRole
from app.services.admin import analytics

NEXT_ORDER_STATUS = {
    OrderStatus.PENDING: OrderStatus.CONFIRMED,
    OrderStatus.CONFIRMED: OrderStatus.SHIPPED,
    OrderStatus.SHIPPED: OrderStatus.DELIVERED,
}

router = APIRouter()


def membership_response(user: User) -> dict[str, str]:
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "status": "active" if user.auth_subject else "pending",
    }


@router.get("/api/admin/users")
async def admin_users(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, str]]:
    require_admin(context)
    users = await session.scalars(
        select(User).where(User.org_id == context.org_id).order_by(User.email)
    )
    return [membership_response(user) for user in users]


@router.post("/api/admin/users", status_code=201)
async def create_admin_user(
    payload: MembershipInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    require_admin(context)
    email = payload.email.strip().lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        # Never says whether the email is in this organization or another one.
        raise HTTPException(status_code=409, detail="This email can't be added")
    user = User(org_id=context.org_id, email=email, role=payload.role)
    session.add(user)
    await session.commit()
    return membership_response(user)


@router.patch("/api/admin/users/{user_id}")
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


@router.delete("/api/admin/users/{user_id}")
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


@router.get("/api/admin/orders")
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


@router.patch("/api/admin/orders/{order_id}/status")
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


@router.get("/api/admin/analytics")
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
