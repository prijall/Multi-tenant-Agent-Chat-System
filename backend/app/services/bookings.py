"""Deterministic availability and booking operations in the organisation time zone."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RequestContext
from app.db.models import (
    Booking,
    BookingStatus,
    Holiday,
    OpeningHours,
    Organization,
    Practitioner,
    PractitionerService,
    Service,
)

SLOT_STEP = timedelta(minutes=30)
# From the studio's cancellation policy (seed/lotus-wellness/kb/cancellation-policy.md).
LATE_CHANGE_WINDOW = timedelta(hours=24)
LATE_FEE_RATE = Decimal("0.5")


class BookingRuleError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def as_utc(value: datetime) -> datetime:
    """SQLite drops time zones on the way back; stored values are always UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def late_fee_for(booking: Booking) -> Decimal:
    """The fee for cancelling or moving this booking now, or a refusal if it has started."""
    time_left = as_utc(booking.starts_at) - datetime.now(UTC)
    if time_left <= timedelta(0):
        raise BookingRuleError("This appointment has already started", 409)
    if time_left >= LATE_CHANGE_WINDOW:
        return Decimal("0.00")
    return (booking.price * LATE_FEE_RATE).quantize(Decimal("0.01"))


async def _organization(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    organization = await session.get(Organization, org_id)
    if organization is None:
        raise BookingRuleError("Organization not found", 404)
    return organization


async def list_services(session: AsyncSession, context: RequestContext) -> list[Service]:
    return list(
        await session.scalars(
            select(Service).where(Service.org_id == context.org_id).order_by(Service.name)
        )
    )


async def list_practitioners(
    session: AsyncSession, context: RequestContext, service_id: uuid.UUID | None = None
) -> list[Practitioner]:
    statement = select(Practitioner).where(Practitioner.org_id == context.org_id)
    if service_id:
        statement = statement.join(PractitionerService).where(
            PractitionerService.service_id == service_id
        )
    return list(await session.scalars(statement.order_by(Practitioner.name)))


async def _validate_entities(
    session: AsyncSession,
    context: RequestContext,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID,
) -> tuple[Service, Practitioner, Organization]:
    service = await session.scalar(
        select(Service).where(Service.id == service_id, Service.org_id == context.org_id)
    )
    practitioner = await session.scalar(
        select(Practitioner).where(
            Practitioner.id == practitioner_id, Practitioner.org_id == context.org_id
        )
    )
    qualified = await session.scalar(
        select(PractitionerService).where(
            PractitionerService.practitioner_id == practitioner_id,
            PractitionerService.service_id == service_id,
        )
    )
    if service is None or practitioner is None or qualified is None:
        raise BookingRuleError("Service or practitioner is unavailable", 404)
    return service, practitioner, await _organization(session, context.org_id)


async def validate_slot(
    session: AsyncSession,
    context: RequestContext,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID,
    starts_at: datetime,
    *,
    exclude_booking_id: uuid.UUID | None = None,
) -> tuple[Service, Practitioner, datetime, datetime, Organization]:
    if starts_at.tzinfo is None:
        raise BookingRuleError("Appointment time must include a time zone")
    service, practitioner, organization = await _validate_entities(
        session, context, service_id, practitioner_id
    )
    timezone = ZoneInfo(organization.timezone)
    local_start = starts_at.astimezone(timezone)
    local_end = local_start + timedelta(minutes=service.duration_minutes)
    if local_start <= datetime.now(timezone):
        raise BookingRuleError("Appointment time must be in the future")
    holiday = await session.scalar(
        select(Holiday).where(Holiday.org_id == context.org_id, Holiday.day == local_start.date())
    )
    hours = await session.scalar(
        select(OpeningHours).where(
            OpeningHours.org_id == context.org_id,
            OpeningHours.weekday == local_start.weekday(),
        )
    )
    if holiday or hours is None:
        raise BookingRuleError("The studio is closed on that day", 409)
    if (
        local_end.date() != local_start.date()
        or local_start.time() < hours.opens_at
        or local_end.time() > hours.closes_at
    ):
        raise BookingRuleError("The appointment does not fit inside opening hours", 409)

    utc_start, utc_end = local_start.astimezone(UTC), local_end.astimezone(UTC)
    conflicts = select(Booking.id).where(
        Booking.org_id == context.org_id,
        Booking.status == BookingStatus.CONFIRMED,
        Booking.starts_at < utc_end,
        Booking.ends_at > utc_start,
        (Booking.practitioner_id == practitioner_id) | (Booking.customer_id == context.user_id),
    )
    if exclude_booking_id:
        conflicts = conflicts.where(Booking.id != exclude_booking_id)
    if await session.scalar(conflicts.limit(1)):
        raise BookingRuleError("That time is no longer available", 409)
    return service, practitioner, utc_start, utc_end, organization


async def available_slots(
    session: AsyncSession,
    context: RequestContext,
    service_id: uuid.UUID,
    start_day: date,
    end_day: date,
    practitioner_id: uuid.UUID | None = None,
    limit: int = 40,
    *,
    exclude_booking_id: uuid.UUID | None = None,
) -> list[dict[str, object]]:
    if end_day < start_day or (end_day - start_day).days > 31:
        raise BookingRuleError("Date range must be between 1 and 31 days")
    service = await session.scalar(
        select(Service).where(Service.id == service_id, Service.org_id == context.org_id)
    )
    if service is None:
        raise BookingRuleError("Service not found", 404)
    practitioners = await list_practitioners(session, context, service_id)
    if practitioner_id:
        practitioners = [item for item in practitioners if item.id == practitioner_id]
    organization = await _organization(session, context.org_id)
    timezone = ZoneInfo(organization.timezone)
    duration = timedelta(minutes=service.duration_minutes)

    # Everything is loaded once and checked in memory; a query per candidate slot is far too slow.
    hours_by_weekday = {
        row.weekday: row
        for row in await session.scalars(
            select(OpeningHours).where(OpeningHours.org_id == context.org_id)
        )
    }
    holidays = set(
        await session.scalars(
            select(Holiday.day).where(
                Holiday.org_id == context.org_id,
                Holiday.day >= start_day,
                Holiday.day <= end_day,
            )
        )
    )
    window_start = datetime.combine(start_day, time.min, timezone)
    window_end = datetime.combine(end_day + timedelta(days=1), time.min, timezone)
    busy_statement = select(Booking).where(
        Booking.org_id == context.org_id,
        Booking.status == BookingStatus.CONFIRMED,
        Booking.starts_at < window_end.astimezone(UTC),
        Booking.ends_at > window_start.astimezone(UTC),
    )
    if exclude_booking_id:
        busy_statement = busy_statement.where(Booking.id != exclude_booking_id)
    busy = [
        (row.practitioner_id, row.customer_id, as_utc(row.starts_at), as_utc(row.ends_at))
        for row in await session.scalars(busy_statement)
    ]
    now = datetime.now(UTC)

    slots: list[dict[str, object]] = []
    day = start_day
    while day <= end_day:
        hours = hours_by_weekday.get(day.weekday())
        if hours and day not in holidays:
            cursor = datetime.combine(day, hours.opens_at, timezone)
            closing = datetime.combine(day, hours.closes_at, timezone)
            while cursor + duration <= closing:
                utc_start = cursor.astimezone(UTC)
                utc_end = (cursor + duration).astimezone(UTC)
                for practitioner in practitioners:
                    taken = any(
                        (held_by == practitioner.id or customer == context.user_id)
                        and starts < utc_end
                        and ends > utc_start
                        for held_by, customer, starts, ends in busy
                    )
                    if utc_start <= now or taken:
                        continue
                    slots.append(
                        {
                            "practitioner_id": str(practitioner.id),
                            "practitioner": practitioner.name,
                            "starts_at": utc_start.isoformat(),
                            "ends_at": utc_end.isoformat(),
                            "local_start": cursor.isoformat(),
                        }
                    )
                    if len(slots) >= limit:
                        return slots
                cursor += SLOT_STEP
        day += timedelta(days=1)
    return slots


async def nearest_slots(
    session: AsyncSession,
    context: RequestContext,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID | None,
    around: datetime,
    *,
    exclude_booking_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict[str, object]]:
    """The free slots closest in time to the one the customer asked for."""
    organization = await _organization(session, context.org_id)
    local_day = around.astimezone(ZoneInfo(organization.timezone)).date()
    candidates = await available_slots(
        session,
        context,
        service_id,
        local_day - timedelta(days=3),
        local_day + timedelta(days=3),
        practitioner_id,
        limit=500,
        exclude_booking_id=exclude_booking_id,
    )
    candidates.sort(key=lambda slot: abs(datetime.fromisoformat(str(slot["starts_at"])) - around))
    return candidates[:limit]


async def create_booking(
    session: AsyncSession,
    context: RequestContext,
    service_id: uuid.UUID,
    practitioner_id: uuid.UUID,
    starts_at: datetime,
    expected_price: Decimal,
) -> Booking:
    service, _, utc_start, utc_end, _ = await validate_slot(
        session, context, service_id, practitioner_id, starts_at
    )
    if service.price != expected_price:
        raise BookingRuleError(
            f"The current price is {service.price}; review and confirm the updated price", 409
        )
    booking = Booking(
        org_id=context.org_id,
        customer_id=context.user_id,
        service_id=service.id,
        practitioner_id=practitioner_id,
        starts_at=utc_start,
        ends_at=utc_end,
        status=BookingStatus.CONFIRMED,
        price=service.price,
    )
    session.add(booking)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise BookingRuleError("That time was just booked; choose another slot", 409) from exc
    return booking


async def customer_bookings(
    session: AsyncSession, context: RequestContext
) -> list[dict[str, object]]:
    rows = await session.execute(
        select(Booking, Service, Practitioner)
        .join(Service, Service.id == Booking.service_id)
        .join(Practitioner, Practitioner.id == Booking.practitioner_id)
        .where(
            Booking.org_id == context.org_id,
            Booking.customer_id == context.user_id,
        )
        .order_by(Booking.starts_at.desc())
    )
    organization = await _organization(session, context.org_id)
    timezone = ZoneInfo(organization.timezone)
    return [
        {
            "id": str(booking.id),
            "service_id": str(service.id),
            "service": service.name,
            "practitioner_id": str(practitioner.id),
            "practitioner": practitioner.name,
            "starts_at": booking.starts_at.isoformat(),
            "local_start": as_utc(booking.starts_at).astimezone(timezone).isoformat(),
            "status": booking.status.value,
            "price": str(booking.price),
            "late_fee": str(booking.late_fee),
        }
        for booking, service, practitioner in rows
    ]


async def cancel_booking(
    session: AsyncSession, context: RequestContext, booking_id: uuid.UUID
) -> Booking:
    booking = await session.scalar(
        select(Booking)
        .where(
            Booking.id == booking_id,
            Booking.org_id == context.org_id,
            Booking.customer_id == context.user_id,
        )
        .with_for_update()
    )
    if booking is None:
        raise BookingRuleError("Booking not found", 404)
    if booking.status is not BookingStatus.CONFIRMED:
        raise BookingRuleError(f"A {booking.status.value} booking cannot be cancelled", 409)
    booking.late_fee += late_fee_for(booking)
    booking.status = BookingStatus.CANCELLED
    await session.commit()
    return booking


async def reschedule_booking(
    session: AsyncSession,
    context: RequestContext,
    booking_id: uuid.UUID,
    starts_at: datetime,
) -> Booking:
    booking = await session.scalar(
        select(Booking)
        .where(
            Booking.id == booking_id,
            Booking.org_id == context.org_id,
            Booking.customer_id == context.user_id,
        )
        .with_for_update()
    )
    if booking is None:
        raise BookingRuleError("Booking not found", 404)
    if booking.status is not BookingStatus.CONFIRMED:
        raise BookingRuleError(f"A {booking.status.value} booking cannot be rescheduled", 409)
    fee = late_fee_for(booking)
    _, _, utc_start, utc_end, _ = await validate_slot(
        session,
        context,
        booking.service_id,
        booking.practitioner_id,
        starts_at,
        exclude_booking_id=booking.id,
    )
    booking.starts_at, booking.ends_at = utc_start, utc_end
    booking.late_fee += fee
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise BookingRuleError("That time was just booked; choose another slot", 409) from exc
    return booking
