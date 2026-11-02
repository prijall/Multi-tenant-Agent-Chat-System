from datetime import datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.actions import propose_order, propose_order_update, propose_reorder
from app.core.security import RequestContext, authenticated_claims, authenticated_subject
from app.db.database import get_session
from app.db.models import (
    Conversation,
    Order,
    OrderStatus,
    Organization,
    Product,
    User,
    UserRole,
)
from app.main import app
from app.services.admin import analytics


@pytest.fixture
async def shop(session):
    org = Organization(slug="himali-tea", name="Himali")
    other = Organization(slug="lotus-wellness", name="Lotus")
    session.add_all([org, other])
    await session.flush()
    admin = User(org_id=org.id, email="admin@test", auth_subject="auth-admin", role=UserRole.ADMIN)
    buyer = User(org_id=org.id, email="buyer@test")
    outsider = User(org_id=other.id, email="secret@lotus.test")
    session.add_all([admin, buyer, outsider])
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=buyer.id, title="Chat")
    session.add_all(
        [
            conversation,
            Product(
                org_id=org.id,
                sku="T1",
                name="Tea",
                category="tea",
                price=Decimal("500"),
                stock=9,
            ),
        ]
    )
    await session.commit()
    return org, admin, buyer, conversation


async def test_revenue_leaves_out_cancelled_orders(session, shop):
    org, admin, buyer, _ = shop
    for status, total in ((OrderStatus.DELIVERED, "300"), (OrderStatus.CANCELLED, "9999")):
        session.add(
            Order(
                org_id=org.id,
                customer_id=buyer.id,
                status=status,
                total=Decimal(total),
                delivery_address="Thamel",
            )
        )
    await session.commit()

    report = await analytics(
        session,
        RequestContext(admin.id, org.id, admin.role),
        datetime(2000, 1, 1),
        datetime(2100, 1, 1),
    )

    assert Decimal(report["order_value"]) == Decimal("300")
    assert Decimal(report["average_order_value"]) == Decimal("300")
    assert {row["status"] for row in report["orders"]} == {"delivered", "cancelled"}


@pytest.fixture
async def client(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def test_admin_cannot_tell_which_organization_has_an_email(shop, client):
    app.dependency_overrides[authenticated_subject] = lambda: "auth-admin"

    in_other_org = await client.post("/api/admin/users", json={"email": "secret@lotus.test"})
    in_own_org = await client.post("/api/admin/users", json={"email": "buyer@test"})

    assert in_other_org.status_code == in_own_org.status_code == 409
    assert in_other_org.json() == in_own_org.json() == {"detail": "This email can't be added"}


async def test_signup_cannot_tell_which_organization_has_an_email(shop, session, client):
    org, *_ = shop
    session.add(User(org_id=org.id, email="taken@test", auth_subject="auth-taken"))
    await session.commit()
    answers = []
    for email in ("secret@lotus.test", "taken@test"):
        app.dependency_overrides[authenticated_claims] = lambda value=email: {
            "sub": "auth-new",
            "email": value,
            "emailVerified": True,
        }
        answers.append(await client.post("/api/provision/himali-tea"))

    assert answers[0].status_code == answers[1].status_code == 409
    assert answers[0].json() == answers[1].json()


async def test_every_order_card_says_the_total_excludes_delivery(session, shop):
    org, _, buyer, conversation = shop
    context = RequestContext(buyer.id, org.id, buyer.role)
    arguments = {"items": [{"sku": "T1", "quantity": 1}], "delivery_address": "Thamel"}
    order = Order(
        org_id=org.id,
        customer_id=buyer.id,
        status=OrderStatus.PENDING,
        total=Decimal("0"),
        delivery_address="Thamel",
    )
    session.add(order)
    await session.commit()
    created = await propose_order(session, context, conversation.id, arguments)

    assert "Rs 150" in created["summary"]["note"]
    assert "Rs 3,000" in created["summary"]["note"]

    changed = await propose_order_update(
        session, context, conversation.id, {**arguments, "order_id": str(order.id)}
    )
    assert "excludes delivery" in changed["summary"]["note"]


async def test_reorder_card_keeps_its_price_note_and_adds_delivery(session, shop):
    org, _, buyer, conversation = shop
    from app.db.models import OrderItem

    product = await session.scalar(Product.__table__.select().where(Product.sku == "T1"))
    order = Order(
        org_id=org.id,
        customer_id=buyer.id,
        status=OrderStatus.DELIVERED,
        total=Decimal("400"),
        delivery_address="Thamel",
    )
    session.add(order)
    await session.flush()
    session.add(
        OrderItem(order_id=order.id, product_id=product, quantity=1, unit_price=Decimal("400"))
    )
    await session.commit()

    result = await propose_reorder(
        session,
        RequestContext(buyer.id, org.id, buyer.role),
        conversation.id,
        {"order_id": str(order.id)},
    )

    assert "was Rs 400" in result["summary"]["note"]
    assert "excludes delivery" in result["summary"]["note"]
