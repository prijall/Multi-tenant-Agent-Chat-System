import json

import httpx
import pytest

from app.db.models import Organization, User, UserRole
from app.scripts import seed


@pytest.fixture
async def org(session):
    organization = Organization(slug="himali-tea", name="Himali Tea House")
    session.add(organization)
    await session.flush()
    session.add(User(org_id=organization.id, email="admin@test", role=UserRole.ADMIN))
    await session.commit()
    return organization


@pytest.fixture
def auth_store(monkeypatch):
    """Stand in for the neon_auth schema, which only exists on Neon."""
    store = {"identities": {}, "verified": set()}

    async def identity_id(_session, email):
        return store["identities"].get(email)

    async def mark_verified(_session, identity):
        store["verified"].add(identity)

    monkeypatch.setattr(seed, "_identity_id", identity_id)
    monkeypatch.setattr(seed, "_mark_verified", mark_verified)
    return store


def auth_client(store, requests):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        store["identities"][body["email"]] = "identity-new"
        return httpx.Response(200, json={})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://auth.example/neondb/auth/",
    )


async def test_new_demo_login_is_created_verified_and_linked(session, org, auth_store):
    requests = []
    async with auth_client(auth_store, requests) as http:
        created = await seed.seed_demo_login(session, http, org, "admin@test", "demo-password")

    assert created is True
    assert str(requests[0].url) == "https://auth.example/neondb/auth/sign-up/email"
    assert auth_store["verified"] == {"identity-new"}
    user = await session.scalar(seed.select(User).where(User.email == "admin@test"))
    assert user.auth_subject == "identity-new"
    assert user.role is UserRole.ADMIN


async def test_existing_login_keeps_its_password_and_is_linked(session, org, auth_store):
    auth_store["identities"]["admin@test"] = "identity-old"
    requests = []
    async with auth_client(auth_store, requests) as http:
        created = await seed.seed_demo_login(session, http, org, "admin@test", "demo-password")

    assert created is False
    assert requests == []
    assert auth_store["verified"] == {"identity-old"}
    user = await session.scalar(seed.select(User).where(User.email == "admin@test"))
    assert user.auth_subject == "identity-old"
