"""Rebuild every organisation from seed/<slug>/: org.json for structured data, kb/ for documents.

Re-running is safe: catalogue data is reset to the seed values, demo customers keep their
history once created, and each knowledge base is dropped and re-embedded.
"""

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.answer_cache import get_answer_cache
from app.core.config import get_settings
from app.db.database import SessionFactory
from app.db.models import (
    Booking,
    BookingStatus,
    Holiday,
    OpeningHours,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusEvent,
    Organization,
    Practitioner,
    PractitionerService,
    Product,
    Service,
    User,
    UserRole,
)
from app.services.knowledge import rebuild_org_knowledge

SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDER_PROGRESSION = [
    (OrderStatus.PENDING, timedelta(0)),
    (OrderStatus.CONFIRMED, timedelta(hours=2)),
    (OrderStatus.SHIPPED, timedelta(days=1)),
    (OrderStatus.DELIVERED, timedelta(days=3)),
]


async def upsert(session: AsyncSession, model, keys: dict, values: dict):
    row = await session.scalar(select(model).filter_by(**keys))
    if row is None:
        row = model(**keys, **values)
        session.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    return row


async def seed_products(session: AsyncSession, org: Organization, products: list[dict]) -> None:
    for item in products:
        await upsert(
            session,
            Product,
            {"org_id": org.id, "sku": item["sku"]},
            {
                "name": item["name"],
                "category": item["category"],
                "price": Decimal(item["price"]),
                "stock": item["stock"],
            },
        )


async def seed_schedule(session: AsyncSession, org: Organization, data: dict) -> None:
    services = {}
    for item in data.get("services", []):
        services[item["code"]] = await upsert(
            session,
            Service,
            {"org_id": org.id, "code": item["code"]},
            {
                "name": item["name"],
                "category": item["category"],
                "duration_minutes": item["duration_minutes"],
                "price": Decimal(item["price"]),
            },
        )
    await session.flush()

    for item in data.get("practitioners", []):
        practitioner = await upsert(
            session,
            Practitioner,
            {"org_id": org.id, "code": item["code"]},
            {"name": item["name"]},
        )
        await session.flush()
        await session.execute(
            delete(PractitionerService).where(
                PractitionerService.practitioner_id == practitioner.id
            )
        )
        session.add_all(
            PractitionerService(practitioner_id=practitioner.id, service_id=services[code].id)
            for code in item["services"]
        )

    if "opening_hours" in data:
        await session.execute(delete(OpeningHours).where(OpeningHours.org_id == org.id))
        session.add_all(
            OpeningHours(
                org_id=org.id,
                weekday=WEEKDAYS.index(item["weekday"]),
                opens_at=time.fromisoformat(item["opens_at"]),
                closes_at=time.fromisoformat(item["closes_at"]),
            )
            for item in data["opening_hours"]
        )
    for item in data.get("holidays", []):
        await upsert(
            session,
            Holiday,
            {"org_id": org.id, "day": date.fromisoformat(item["day"])},
            {"name": item["name"]},
        )


async def seed_orders(session: AsyncSession, org: Organization, user: User, orders: list[dict]):
    products = {
        row.sku: row
        for row in await session.scalars(select(Product).where(Product.org_id == org.id))
    }
    now = datetime.now(UTC)
    for item in orders:
        created_at = now - timedelta(days=item["days_ago"])
        status = OrderStatus(item["status"])
        order_items = [
            OrderItem(
                product_id=products[line["sku"]].id,
                quantity=line["quantity"],
                unit_price=products[line["sku"]].price,
            )
            for line in item["items"]
        ]
        reached = ORDER_PROGRESSION[: [step for step, _ in ORDER_PROGRESSION].index(status) + 1]
        session.add(
            Order(
                org_id=org.id,
                customer_id=user.id,
                status=status,
                delivery_address=item["delivery_address"],
                total=sum(line.unit_price * line.quantity for line in order_items),
                created_at=created_at,
                items=order_items,
                history=[
                    OrderStatusEvent(status=step, at=min(created_at + offset, now))
                    for step, offset in reached
                ],
            )
        )


async def seed_bookings(
    session: AsyncSession, org: Organization, user: User, bookings: list[dict]
) -> None:
    zone = ZoneInfo(org.timezone)
    services = {
        row.code: row
        for row in await session.scalars(select(Service).where(Service.org_id == org.id))
    }
    practitioners = {
        row.code: row
        for row in await session.scalars(
            select(Practitioner).where(Practitioner.org_id == org.id)
        )
    }
    open_days = {
        row.weekday
        for row in await session.scalars(
            select(OpeningHours).where(OpeningHours.org_id == org.id)
        )
    }
    holidays = set(
        await session.scalars(select(Holiday.day).where(Holiday.org_id == org.id))
    )
    today = datetime.now(zone).date()
    week_start = today - timedelta(days=today.weekday())
    for item in bookings:
        day = (
            week_start
            + timedelta(weeks=item["weeks_from_now"])
            + timedelta(days=WEEKDAYS.index(item["weekday"]))
        )
        # Seed dates are relative, so step past any closure they happen to land on.
        while day in holidays or day.weekday() not in open_days:
            day += timedelta(days=1)
        service = services[item["service"]]
        starts_at = datetime.combine(day, time.fromisoformat(item["time"]), tzinfo=zone)
        session.add(
            Booking(
                org_id=org.id,
                customer_id=user.id,
                service_id=service.id,
                practitioner_id=practitioners[item["practitioner"]].id,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=service.duration_minutes),
                status=BookingStatus(item["status"]),
                price=service.price,
            )
        )


async def seed_customers(session: AsyncSession, org: Organization, customers: list[dict]):
    for item in customers:
        user = await upsert(
            session,
            User,
            {"org_id": org.id, "email": item["email"]},
            {"role": UserRole(item["role"])},
        )
        await session.flush()
        if item.get("orders") and not await session.scalar(
            select(Order.id).where(Order.customer_id == user.id).limit(1)
        ):
            await seed_orders(session, org, user, item["orders"])
        if item.get("bookings") and not await session.scalar(
            select(Booking.id).where(Booking.customer_id == user.id).limit(1)
        ):
            await seed_bookings(session, org, user, item["bookings"])


async def _identity_id(session: AsyncSession, email: str) -> str | None:
    return await session.scalar(
        text('SELECT id::text FROM neon_auth."user" WHERE lower(email) = :email'),
        {"email": email.lower()},
    )


async def _mark_verified(session: AsyncSession, identity_id: str) -> None:
    # Demo addresses can't receive mail, so the seed vouches for them directly.
    await session.execute(
        text('UPDATE neon_auth."user" SET "emailVerified" = true WHERE id::text = :id'),
        {"id": identity_id},
    )


async def seed_demo_login(
    session: AsyncSession,
    http: httpx.AsyncClient,
    org: Organization,
    email: str,
    password: str,
) -> bool:
    """Give one demo user a working login; returns False if an existing password was kept."""
    identity_id = await _identity_id(session, email)
    created = identity_id is None
    if created:
        response = await http.post(
            "/sign-up/email",
            json={"name": email.split("@")[0], "email": email, "password": password},
        )
        response.raise_for_status()
        identity_id = await _identity_id(session, email)
        if identity_id is None:
            raise RuntimeError(f"Neon Auth did not create a login for {email}")
    await _mark_verified(session, identity_id)
    user = await session.scalar(select(User).where(User.org_id == org.id, User.email == email))
    if user.auth_subject is None:
        user.auth_subject = identity_id
    return created


async def seed_demo_logins(session: AsyncSession, org: Organization, customers: list[dict]) -> None:
    settings = get_settings()
    if not settings.demo_password or not settings.neon_auth_base_url:
        print(f"{org.name}: skipped demo logins (DEMO_PASSWORD or NEON_AUTH_BASE_URL is not set)")
        return
    async with httpx.AsyncClient(
        base_url=settings.neon_auth_base_url.rstrip("/") + "/",
        headers={"Origin": settings.frontend_origin},
        timeout=30,
    ) as http:
        for item in customers:
            created = await seed_demo_login(
                session, http, org, item["email"], settings.demo_password
            )
            if not created:
                print(f"{org.name}: {item['email']} already has a login; kept its password")


async def seed_organization(session: AsyncSession, directory: Path) -> str:
    data = json.loads((directory / "org.json").read_text(encoding="utf-8"))
    org = await upsert(
        session,
        Organization,
        {"slug": data["slug"]},
        {"name": data["name"], "timezone": data.get("timezone", "Asia/Kathmandu")},
    )
    await session.flush()
    await seed_products(session, org, data.get("products", []))
    await seed_schedule(session, org, data)
    await session.flush()
    await seed_customers(session, org, data.get("customers", []))
    chunks = await rebuild_org_knowledge(session, org, directory / "kb")
    # New documents make yesterday's answers stale.
    if cache := get_answer_cache():
        await cache.purge(org.id)
    await seed_demo_logins(session, org, data.get("customers", []))
    return (
        f"{org.name}: {len(data.get('products', []))} products, "
        f"{len(data.get('services', []))} services, "
        f"{len(data.get('customers', []))} users, {chunks} knowledge chunks"
    )


async def seed() -> None:
    for directory in sorted(path for path in SEED_DIR.iterdir() if path.is_dir()):
        async with SessionFactory() as session:
            summary = await seed_organization(session, directory)
            await session.commit()
        print(summary)


if __name__ == "__main__":
    asyncio.run(seed())
