import pytest
from fastapi import HTTPException

from app.core.security import authenticated_claims, resolve_context_from_session
from app.db.models import Organization, User, UserRole


@pytest.mark.asyncio
async def test_managed_auth_subject_resolves_application_tenant_and_role(session):
    organization = Organization(slug="himali-tea", name="Himali Tea House")
    session.add(organization)
    await session.flush()
    user = User(
        org_id=organization.id,
        auth_subject="managed-auth-user-123",
        email="customer@himali.test",
        role=UserRole.CUSTOMER,
    )
    session.add(user)
    await session.commit()

    context = await resolve_context_from_session(session, "managed-auth-user-123")

    assert context.user_id == user.id
    assert context.org_id == organization.id
    assert context.role is UserRole.CUSTOMER


@pytest.mark.asyncio
async def test_missing_bearer_token_is_rejected():
    with pytest.raises(HTTPException) as error:
        await authenticated_claims(None)

    assert error.value.status_code == 401
