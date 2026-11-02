from decimal import Decimal

import pytest

from app.core.security import RequestContext
from app.db.models import Order, Organization, Product, User, UserRole
from app.db.repositories import get_customer_order, list_products


@pytest.mark.asyncio
async def test_product_search_returns_only_authenticated_organization(session):
    himali = Organization(slug="himali-tea", name="Himali Tea House")
    lotus = Organization(slug="lotus-wellness", name="Lotus Wellness Studio")
    session.add_all([himali, lotus])
    await session.flush()

    customer = User(
        org_id=himali.id,
        auth_subject="auth-himali-customer",
        email="customer@himali.test",
        role=UserRole.CUSTOMER,
    )
    session.add_all(
        [
            customer,
            Product(
                org_id=himali.id,
                sku="HT-001",
                name="Himalayan Black Tea",
                category="tea",
                price=Decimal("600.00"),
                stock=10,
            ),
            Product(
                org_id=lotus.id,
                sku="SECRET-001",
                name="Lotus Internal Product",
                category="private",
                price=Decimal("1.00"),
                stock=1,
            ),
        ]
    )
    await session.commit()

    context = RequestContext(customer.id, himali.id, UserRole.CUSTOMER)
    products = await list_products(session, context)

    assert [product.sku for product in products] == ["HT-001"]


@pytest.mark.asyncio
async def test_customer_cannot_read_another_customers_order(session):
    org = Organization(slug="himali-tea", name="Himali Tea House")
    session.add(org)
    await session.flush()
    alice = User(org_id=org.id, auth_subject="auth-alice", email="alice@test")
    bob = User(org_id=org.id, auth_subject="auth-bob", email="bob@test")
    session.add_all([alice, bob])
    await session.flush()
    bobs_order = Order(
        org_id=org.id,
        customer_id=bob.id,
        total=Decimal("600.00"),
        delivery_address="Kathmandu",
    )
    session.add(bobs_order)
    await session.commit()

    alice_context = RequestContext(alice.id, org.id, UserRole.CUSTOMER)

    assert await get_customer_order(session, alice_context, bobs_order.id) is None


@pytest.mark.asyncio
async def test_customer_cannot_read_cross_organization_order(session):
    himali = Organization(slug="himali-tea", name="Himali Tea House")
    lotus = Organization(slug="lotus-wellness", name="Lotus Wellness Studio")
    session.add_all([himali, lotus])
    await session.flush()
    himali_user = User(
        org_id=himali.id,
        auth_subject="auth-himali",
        email="himali@test",
    )
    lotus_user = User(
        org_id=lotus.id,
        auth_subject="auth-lotus",
        email="lotus@test",
    )
    session.add_all([himali_user, lotus_user])
    await session.flush()
    lotus_order = Order(
        org_id=lotus.id,
        customer_id=lotus_user.id,
        total=Decimal("1000.00"),
        delivery_address="Lalitpur",
    )
    session.add(lotus_order)
    await session.commit()

    context = RequestContext(himali_user.id, himali.id, UserRole.CUSTOMER)

    assert await get_customer_order(session, context, lotus_order.id) is None
