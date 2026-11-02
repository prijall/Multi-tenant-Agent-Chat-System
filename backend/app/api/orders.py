import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_context
from app.api.schemas import OrderConfirmInput, OrderQuoteInput, ReorderInput
from app.core.security import RequestContext
from app.db.database import get_session
from app.db.repositories import list_customer_orders, list_products
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

router = APIRouter()


def requested_items(payload: OrderQuoteInput) -> list[RequestedItem]:
    return [RequestedItem(item.sku, item.quantity) for item in payload.items]


def order_http_error(exc: OrderRuleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/api/products")
async def products(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await list_products(session, context)
    return [
        {"sku": row.sku, "name": row.name, "price": str(row.price), "stock": row.stock}
        for row in rows
    ]


@router.get("/api/orders")
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


@router.get("/api/orders/{order_id}")
async def order(
    order_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        return await order_detail(session, context, order_id)
    except OrderRuleError as exc:
        raise order_http_error(exc) from exc


@router.post("/api/order-quotes")
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


@router.post("/api/orders", status_code=201)
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


@router.put("/api/orders/{order_id}")
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


@router.delete("/api/orders/{order_id}")
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


@router.post("/api/orders/{order_id}/reorder", status_code=201)
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


@router.get("/api/orders/{order_id}/reorder-quote")
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
