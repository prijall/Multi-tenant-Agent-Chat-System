"""Deterministic, tenant-scoped order lifecycle operations."""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import RequestContext
from app.db.models import Order, OrderItem, OrderStatus, OrderStatusEvent, Product


class OrderRuleError(ValueError):
    def __init__(self, message: str, status_code: int = 400, *, short_sku: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.short_sku = short_sku


@dataclass(frozen=True, slots=True)
class RequestedItem:
    sku: str
    quantity: int


def _normalise(items: list[RequestedItem]) -> dict[str, int]:
    quantities: dict[str, int] = {}
    for item in items:
        sku = item.sku.strip().upper()
        if not sku or item.quantity <= 0:
            raise OrderRuleError("Every item needs a SKU and a positive quantity")
        quantities[sku] = quantities.get(sku, 0) + item.quantity
    if not quantities:
        raise OrderRuleError("An order needs at least one item")
    return quantities


async def quote_order(
    session: AsyncSession,
    context: RequestContext,
    items: list[RequestedItem],
    *,
    lock: bool = False,
    stock_credit: dict[uuid.UUID, int] | None = None,
) -> tuple[list[dict[str, object]], Decimal, dict[str, Product]]:
    quantities = _normalise(items)
    statement = select(Product).where(
        Product.org_id == context.org_id, Product.sku.in_(quantities)
    )
    if lock:
        statement = statement.with_for_update()
    products = {product.sku: product for product in await session.scalars(statement)}
    missing = sorted(set(quantities) - set(products))
    if missing:
        raise OrderRuleError(f"Unknown product: {', '.join(missing)}", 404)

    lines: list[dict[str, object]] = []
    total = Decimal("0")
    for sku, quantity in quantities.items():
        product = products[sku]
        available = product.stock + (stock_credit or {}).get(product.id, 0)
        if quantity > available:
            raise OrderRuleError(
                f"{product.name} has only {available} in stock", 409, short_sku=sku
            )
        line_total = product.price * quantity
        total += line_total
        lines.append(
            {
                "sku": sku,
                "name": product.name,
                "quantity": quantity,
                "unit_price": str(product.price),
                "line_total": str(line_total),
                "available_stock": available,
            }
        )
    return lines, total, products


async def quote_order_change(
    session: AsyncSession,
    context: RequestContext,
    order_id: uuid.UUID,
    items: list[RequestedItem],
) -> tuple[list[dict[str, object]], Decimal]:
    """Quote an edit, counting the stock this pending order already holds as available."""
    detail = await order_detail(session, context, order_id)
    if detail["status"] != OrderStatus.PENDING.value:
        raise OrderRuleError(f"A {detail['status']} order cannot be changed", 409)
    credit: dict[uuid.UUID, int] = {}
    for item in await session.scalars(select(OrderItem).where(OrderItem.order_id == order_id)):
        credit[item.product_id] = credit.get(item.product_id, 0) + item.quantity
    lines, total, _ = await quote_order(session, context, items, stock_credit=credit)
    return lines, total


async def alternatives_for(
    session: AsyncSession, context: RequestContext, sku: str, limit: int = 3
) -> list[dict[str, object]]:
    """In-stock products from the same category as one that is short."""
    category = await session.scalar(
        select(Product.category).where(Product.org_id == context.org_id, Product.sku == sku)
    )
    rows = await session.scalars(
        select(Product)
        .where(
            Product.org_id == context.org_id,
            Product.category == category,
            Product.sku != sku,
            Product.stock > 0,
        )
        .order_by(Product.stock.desc())
        .limit(limit)
    )
    return [
        {"sku": row.sku, "name": row.name, "price": str(row.price), "stock": row.stock}
        for row in rows
    ]


async def create_order(
    session: AsyncSession,
    context: RequestContext,
    items: list[RequestedItem],
    delivery_address: str,
    expected_total: Decimal,
) -> Order:
    if not delivery_address.strip():
        raise OrderRuleError("Delivery address is required")
    lines, total, products = await quote_order(session, context, items, lock=True)
    if total != expected_total:
        raise OrderRuleError(
            f"The current total is {total}; review and confirm the updated price", 409
        )
    order = Order(
        org_id=context.org_id,
        customer_id=context.user_id,
        status=OrderStatus.PENDING,
        total=total,
        delivery_address=delivery_address.strip(),
    )
    session.add(order)
    await session.flush()
    for line in lines:
        product = products[str(line["sku"])]
        quantity = int(line["quantity"])
        product.stock -= quantity
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=product.price,
            )
        )
    session.add(OrderStatusEvent(order_id=order.id, status=OrderStatus.PENDING))
    await session.commit()
    return order


async def update_pending_order(
    session: AsyncSession,
    context: RequestContext,
    order_id: uuid.UUID,
    items: list[RequestedItem],
    delivery_address: str,
    expected_total: Decimal,
) -> Order:
    order = await session.scalar(
        select(Order)
        .where(
            Order.id == order_id,
            Order.org_id == context.org_id,
            Order.customer_id == context.user_id,
        )
        .with_for_update()
    )
    if order is None:
        raise OrderRuleError("Order not found", 404)
    if order.status is not OrderStatus.PENDING:
        raise OrderRuleError(f"A {order.status.value} order cannot be changed", 409)
    old_items = list(
        await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
    )
    credit: dict[uuid.UUID, int] = {}
    for item in old_items:
        credit[item.product_id] = credit.get(item.product_id, 0) + item.quantity
    lines, total, products = await quote_order(
        session, context, items, lock=True, stock_credit=credit
    )
    if total != expected_total:
        raise OrderRuleError(
            f"The current total is {total}; review and confirm the updated price", 409
        )

    all_product_ids = set(credit) | {product.id for product in products.values()}
    locked = {
        product.id: product
        for product in await session.scalars(
            select(Product).where(Product.id.in_(all_product_ids)).with_for_update()
        )
    }
    for product_id, quantity in credit.items():
        locked[product_id].stock += quantity
    await session.execute(delete(OrderItem).where(OrderItem.order_id == order.id))
    for line in lines:
        product = products[str(line["sku"])]
        quantity = int(line["quantity"])
        locked[product.id].stock -= quantity
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=product.price,
            )
        )
    order.total = total
    order.delivery_address = delivery_address.strip()
    await session.commit()
    return order


async def cancel_pending_order(
    session: AsyncSession, context: RequestContext, order_id: uuid.UUID
) -> Order:
    order = await session.scalar(
        select(Order)
        .where(
            Order.id == order_id,
            Order.org_id == context.org_id,
            Order.customer_id == context.user_id,
        )
        .with_for_update()
    )
    if order is None:
        raise OrderRuleError("Order not found", 404)
    if order.status is not OrderStatus.PENDING:
        raise OrderRuleError(f"A {order.status.value} order cannot be cancelled", 409)
    items = list(await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)))
    products = {
        product.id: product
        for product in await session.scalars(
            select(Product)
            .where(Product.id.in_([item.product_id for item in items]))
            .with_for_update()
        )
    }
    for item in items:
        products[item.product_id].stock += item.quantity
    order.status = OrderStatus.CANCELLED
    session.add(OrderStatusEvent(order_id=order.id, status=OrderStatus.CANCELLED))
    await session.commit()
    return order


async def order_detail(
    session: AsyncSession, context: RequestContext, order_id: uuid.UUID
) -> dict[str, object]:
    order = await session.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.history))
        .where(
            Order.id == order_id,
            Order.org_id == context.org_id,
            Order.customer_id == context.user_id,
        )
    )
    if order is None:
        raise OrderRuleError("Order not found", 404)
    products = {
        product.id: product
        for product in await session.scalars(
            select(Product).where(Product.id.in_([item.product_id for item in order.items]))
        )
    }
    return {
        "id": str(order.id),
        "status": order.status.value,
        "total": str(order.total),
        "delivery_address": order.delivery_address,
        "payment_method": order.payment_method,
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "sku": products[item.product_id].sku,
                "name": products[item.product_id].name,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
            }
            for item in order.items
        ],
        "history": [
            {"status": event.status.value, "at": event.at.isoformat()}
            for event in order.history
        ],
    }


async def reorder(
    session: AsyncSession,
    context: RequestContext,
    source_order_id: uuid.UUID,
    expected_total: Decimal,
) -> Order:
    detail = await order_detail(session, context, source_order_id)
    return await create_order(
        session,
        context,
        [RequestedItem(str(item["sku"]), int(item["quantity"])) for item in detail["items"]],
        str(detail["delivery_address"]),
        expected_total,
    )
