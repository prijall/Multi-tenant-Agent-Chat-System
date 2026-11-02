import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import RequestContext
from app.db.models import Order, Product


async def list_products(session: AsyncSession, context: RequestContext) -> list[Product]:
    result = await session.scalars(
        select(Product).where(Product.org_id == context.org_id).order_by(Product.name)
    )
    return list(result)


async def get_customer_order(
    session: AsyncSession,
    context: RequestContext,
    order_id: uuid.UUID,
) -> Order | None:
    return await session.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(
            Order.id == order_id,
            Order.org_id == context.org_id,
            Order.customer_id == context.user_id,
        )
    )


async def list_customer_orders(session: AsyncSession, context: RequestContext) -> list[Order]:
    result = await session.scalars(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.org_id == context.org_id, Order.customer_id == context.user_id)
        .order_by(Order.created_at.desc())
    )
    return list(result)

