import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_context
from app.api.schemas import BookingConfirmInput, BookingQuoteInput, RescheduleInput
from app.core.security import RequestContext
from app.db.database import get_session
from app.db.models import Booking
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

router = APIRouter()


def booking_http_error(exc: BookingRuleError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/api/services")
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


@router.get("/api/practitioners")
async def practitioners(
    service_id: uuid.UUID | None = None,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, str]]:
    rows = await list_practitioners(session, context, service_id)
    return [{"id": str(row.id), "code": row.code, "name": row.name} for row in rows]


@router.get("/api/availability")
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


@router.post("/api/booking-quotes")
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


@router.get("/api/bookings")
async def bookings(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    return await customer_bookings(session, context)


@router.post("/api/bookings", status_code=201)
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


@router.delete("/api/bookings/{booking_id}")
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


@router.put("/api/bookings/{booking_id}")
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
