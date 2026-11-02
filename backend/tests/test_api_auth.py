import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import authenticated_claims, authenticated_subject
from app.db.database import get_session
from app.db.models import Order, Organization, User, UserRole
from app.main import app


@pytest.fixture
async def orgs(session):
    himali = Organization(slug="himali-tea", name="Himali Tea House")
    lotus = Organization(slug="lotus-wellness", name="Lotus Wellness Studio")
    session.add_all([himali, lotus])
    await session.commit()
    return himali, lotus


@pytest.fixture
async def client(session):
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def sign_in_as(subject: str, email: str | None = None, **extra_claims: object) -> None:
    """Stand in for a verified Neon Auth token carrying this identity."""
    claims = {"sub": subject, "email": email, "emailVerified": True, **extra_claims}
    app.dependency_overrides[authenticated_subject] = lambda: subject
    app.dependency_overrides[authenticated_claims] = lambda: claims


async def test_me_reports_the_stored_org_whatever_page_was_used(session, orgs, client):
    himali, _ = orgs
    session.add(User(org_id=himali.id, auth_subject="auth-priya", email="priya@test"))
    await session.commit()
    sign_in_as("auth-priya")

    response = await client.get("/api/me", headers={"X-Org": "lotus-wellness"})

    assert response.json() == {"organization": "himali-tea", "role": "customer"}


async def test_org_entry_point_fixes_signup_membership(session, orgs, client):
    himali, _ = orgs
    session.add(User(org_id=himali.id, email="priya@test"))
    await session.commit()
    sign_in_as("auth-priya", "priya@test")

    response = await client.post("/api/provision/himali-tea")

    assert response.json() == {
        "status": "linked",
        "organization": "himali-tea",
        "role": "customer",
    }


@pytest.mark.parametrize("claims", [{"emailVerified": False}, {"emailVerified": None}])
async def test_unverified_identity_cannot_claim_a_pending_admin_profile(
    session, orgs, client, claims
):
    himali, _ = orgs
    pending = User(org_id=himali.id, email="admin@test", role=UserRole.ADMIN)
    session.add(pending)
    await session.commit()
    sign_in_as("auth-attacker", "admin@test", **claims)

    response = await client.post("/api/provision/himali-tea")

    assert response.status_code == 403
    await session.refresh(pending)
    assert pending.auth_subject is None


async def test_unverified_new_customer_can_still_sign_up(session, orgs, client):
    sign_in_as("auth-new", "new@test", emailVerified=False)

    response = await client.post("/api/provision/himali-tea")

    assert response.json() == {
        "status": "created",
        "organization": "himali-tea",
        "role": "customer",
    }


async def test_signup_cannot_cross_from_one_org_entry_point_to_another(session, orgs, client):
    himali, _ = orgs
    session.add(User(org_id=himali.id, email="priya@test"))
    await session.commit()
    sign_in_as("auth-priya", "priya@test")

    response = await client.post("/api/provision/lotus-wellness")

    assert response.status_code == 409


async def test_cross_org_order_id_in_url_is_not_found(session, orgs, client):
    himali, lotus = orgs
    himali_user = User(org_id=himali.id, auth_subject="auth-himali", email="h@test")
    lotus_user = User(org_id=lotus.id, auth_subject="auth-lotus", email="l@test")
    session.add_all([himali_user, lotus_user])
    await session.flush()
    lotus_order = Order(
        org_id=lotus.id, customer_id=lotus_user.id, total=Decimal("10"), delivery_address="x"
    )
    session.add(lotus_order)
    await session.commit()
    sign_in_as("auth-himali")

    response = await client.get(f"/api/orders/{lotus_order.id}")

    assert response.status_code == 404


async def test_admin_can_add_user_only_to_their_organization(session, orgs, client):
    himali, lotus = orgs
    admin = User(
        org_id=himali.id,
        auth_subject="auth-admin",
        email="admin.himali@example.com",
        role=UserRole.ADMIN,
    )
    session.add(admin)
    await session.commit()
    sign_in_as("auth-admin", "admin.himali@example.com")

    response = await client.post(
        "/api/admin/users", json={"email": "new.customer@example.com", "role": "customer"}
    )

    assert response.status_code == 201
    created = await session.get(User, uuid.UUID(response.json()["id"]))
    assert created.org_id == himali.id
    assert created.org_id != lotus.id
    assert created.auth_subject is None
