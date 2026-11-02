from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.security import RequestContext
from app.db.models import (
    BookingStatus,
    OpeningHours,
    Organization,
    Practitioner,
    PractitionerService,
    Product,
    Service,
    User,
)
from app.services.bookings import BookingRuleError, cancel_booking, create_booking
from app.services.orders import (
    OrderRuleError,
    RequestedItem,
    cancel_pending_order,
    create_order,
    quote_order,
)


@pytest.fixture
async def store_context(session):
    org = Organization(slug="himali", name="Himali")
    session.add(org)
    await session.flush()
    customer = User(org_id=org.id, email="customer@himali.test")
    product = Product(
        org_id=org.id,
        sku="TEA-1",
        name="Test tea",
        category="tea",
        price=Decimal("500.00"),
        stock=3,
    )
    session.add_all([customer, product])
    await session.commit()
    return RequestContext(customer.id, org.id, customer.role), product


@pytest.fixture
async def studio_context(session):
    org = Organization(slug="lotus", name="Lotus", timezone="UTC")
    session.add(org)
    await session.flush()
    first = User(org_id=org.id, email="first@lotus.test")
    second = User(org_id=org.id, email="second@lotus.test")
    service = Service(
        org_id=org.id,
        code="massage",
        name="Test massage",
        category="massage",
        duration_minutes=60,
        price=Decimal("1000.00"),
    )
    practitioner = Practitioner(org_id=org.id, code="practitioner", name="Test practitioner")
    session.add_all([first, second, service, practitioner])
    await session.flush()
    session.add(PractitionerService(practitioner_id=practitioner.id, service_id=service.id))
    for weekday in range(7):
        session.add(
            OpeningHours(org_id=org.id, weekday=weekday, opens_at=time(8), closes_at=time(18))
        )
    await session.commit()
    return (
        RequestContext(first.id, org.id, first.role),
        RequestContext(second.id, org.id, second.role),
        service,
        practitioner,
    )


async def test_order_requires_current_quote_and_reduces_stock(session, store_context):
    context, product = store_context
    items = [RequestedItem("tea-1", 2)]
    lines, total, _ = await quote_order(session, context, items)

    assert lines[0]["available_stock"] == 3
    order = await create_order(session, context, items, "Kathmandu", total)

    await session.refresh(product)
    assert order.total == Decimal("1000.00")
    assert product.stock == 1


async def test_order_rejects_stale_price_or_insufficient_stock(session, store_context):
    context, product = store_context
    with pytest.raises(OrderRuleError, match="only 3"):
        await quote_order(session, context, [RequestedItem("TEA-1", 4)])

    with pytest.raises(OrderRuleError, match="current total"):
        await create_order(session, context, [RequestedItem("TEA-1", 1)], "Kathmandu", Decimal("1"))
    await session.refresh(product)
    assert product.stock == 3


async def test_pending_order_cancellation_restores_stock(session, store_context):
    context, product = store_context
    order = await create_order(
        session, context, [RequestedItem("TEA-1", 2)], "Kathmandu", Decimal("1000")
    )
    await cancel_pending_order(session, context, order.id)

    await session.refresh(product)
    assert product.stock == 3


async def test_booking_rejects_practitioner_overlap(session, studio_context):
    first, second, service, practitioner = studio_context
    starts_at = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )
    first_booking = await create_booking(
        session, first, service.id, practitioner.id, starts_at, Decimal("1000")
    )

    with pytest.raises(BookingRuleError, match="no longer available"):
        await create_booking(
            session, second, service.id, practitioner.id, starts_at, Decimal("1000")
        )

    assert first_booking.status is BookingStatus.CONFIRMED


async def test_booking_rejects_customer_overlap_and_cancel_releases_slot(session, studio_context):
    first, _, service, practitioner = studio_context
    starts_at = datetime.now(UTC).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(
        days=3
    )
    booking = await create_booking(
        session, first, service.id, practitioner.id, starts_at, Decimal("1000")
    )
    with pytest.raises(BookingRuleError, match="no longer available"):
        await create_booking(
            session,
            first,
            service.id,
            practitioner.id,
            starts_at + timedelta(minutes=30),
            Decimal("1000"),
        )

    await cancel_booking(session, first, booking.id)
    replacement = await create_booking(
        session, first, service.id, practitioner.id, starts_at, Decimal("1000")
    )
    assert replacement.status is BookingStatus.CONFIRMED
