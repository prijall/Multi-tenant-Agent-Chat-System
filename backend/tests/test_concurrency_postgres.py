"""Real races on real Postgres: SQLite has no exclusion constraints and ignores row locks.

Runs in a private schema that is dropped afterwards, the same way the evals do.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import RequestContext
from app.db.models import (
    Booking,
    BookingStatus,
    Order,
    Organization,
    Practitioner,
    Product,
    Service,
    User,
)
from app.services.bookings import BookingRuleError, create_booking, reschedule_booking
from app.services.orders import OrderRuleError, RequestedItem, create_order
from evals.harness import EvalDatabase, World, local

pytestmark = pytest.mark.skipif(
    get_settings().migration_database_url.startswith("sqlite"),
    reason="these races need Postgres",
)

LOTUS_CUSTOMERS = ["anita.lotus@example.com", "rohan.lotus@example.com", "kiran.lotus@example.com"]
HIMALI_CUSTOMERS = [
    "priya.himali@example.com",
    "bikash.himali@example.com",
    "sanjay.himali@example.com",
]


@pytest.fixture
async def database():
    eval_database = EvalDatabase(get_settings().migration_database_url)
    try:
        await eval_database.create(with_knowledge=False)
        await eval_database.reset()
        yield eval_database
    finally:
        await eval_database.drop()


async def contexts(database: EvalDatabase, slug: str, emails: list[str]):
    async with database.sessions() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == slug))
        users = [await session.scalar(select(User).where(User.email == email)) for email in emails]
        return org.id, [RequestContext(user.id, org.id, user.role) for user in users]


async def studio(database: EvalDatabase):
    org_id, customers = await contexts(database, "lotus-wellness", LOTUS_CUSTOMERS)
    async with database.sessions() as session:
        service = await session.scalar(
            select(Service).where(Service.org_id == org_id, Service.code == "swedish-60")
        )
        practitioner = await session.scalar(
            select(Practitioner).where(
                Practitioner.org_id == org_id, Practitioner.code == "sita-gurung"
            )
        )
    world = World(database.sessions, None, org_id, customers[0].user_id, customers[0], None)
    day = await world.open_day(weekdays={0, 1, 2, 3}, after=14)
    return org_id, customers, service, practitioner, day


async def confirmed_at(database: EvalDatabase, practitioner_id, starts_at) -> list[Booking]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(Booking).where(
                    Booking.practitioner_id == practitioner_id,
                    Booking.starts_at == starts_at,
                    Booking.status == BookingStatus.CONFIRMED,
                )
            )
        )


async def test_three_customers_racing_for_one_slot_leaves_exactly_one_booking(database):
    _, customers, service, practitioner, day = await studio(database)
    starts_at = local(day, 10)

    async def attempt(context):
        async with database.sessions() as session:
            return await create_booking(
                session, context, service.id, practitioner.id, starts_at, service.price
            )

    results = await asyncio.gather(
        *(attempt(context) for context in customers), return_exceptions=True
    )

    won = [result for result in results if isinstance(result, Booking)]
    refused = [result for result in results if isinstance(result, BookingRuleError)]
    assert len(won) == 1
    assert len(refused) == 2
    assert all(error.status_code == 409 for error in refused)
    assert len(await confirmed_at(database, practitioner.id, starts_at)) == 1


async def test_two_customers_rescheduling_into_one_slot_leaves_exactly_one_there(database):
    _, customers, service, practitioner, day = await studio(database)
    target = local(day, 15)
    bookings = []
    for hour, context in zip((9, 11), customers[:2], strict=True):
        async with database.sessions() as session:
            booking = await create_booking(
                session, context, service.id, practitioner.id, local(day, hour), service.price
            )
            bookings.append((context, booking.id))

    async def attempt(context, booking_id):
        async with database.sessions() as session:
            return await reschedule_booking(session, context, booking_id, target)

    results = await asyncio.gather(
        *(attempt(context, booking_id) for context, booking_id in bookings),
        return_exceptions=True,
    )

    assert sum(isinstance(result, Booking) for result in results) == 1
    assert sum(isinstance(result, BookingRuleError) for result in results) == 1
    assert len(await confirmed_at(database, practitioner.id, target)) == 1


async def test_buyers_racing_for_the_last_units_never_oversell(database):
    org_id, customers = await contexts(database, "himali-tea", HIMALI_CUSTOMERS)
    expected = Decimal("4400.00")

    async def attempt(context):
        async with database.sessions() as session:
            return await create_order(
                session, context, [RequestedItem("TW-002", 2)], "Thamel, Kathmandu", expected
            )

    results = await asyncio.gather(
        *(attempt(context) for context in customers), return_exceptions=True
    )

    assert sum(isinstance(result, Order) for result in results) == 1
    assert sum(isinstance(result, OrderRuleError) for result in results) == 2
    async with database.sessions() as session:
        stock = await session.scalar(
            select(Product.stock).where(Product.org_id == org_id, Product.sku == "TW-002")
        )
    assert stock == 0
