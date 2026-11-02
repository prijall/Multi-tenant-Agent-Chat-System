from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_context
from app.core.security import RequestContext, authenticated_claims
from app.db.database import get_session
from app.db.models import Organization, User, UserRole

router = APIRouter()


@router.post("/api/provision/{organization_slug}")
async def provision_customer_profile(
    organization_slug: str,
    claims: Annotated[dict[str, Any], Depends(authenticated_claims)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Provision signup into the organization fixed by its branded entry point."""
    organization = await session.scalar(
        select(Organization).where(Organization.slug == organization_slug)
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    subject = str(claims["sub"])
    existing_identity = await session.scalar(
        select(User).where(User.auth_subject == subject)
    )
    if existing_identity is not None:
        if existing_identity.org_id != organization.id:
            raise HTTPException(
                status_code=409,
                detail="Identity belongs to another organization",
            )
        return {
            "status": "ready",
            "organization": organization.slug,
            "role": existing_identity.role,
        }

    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="The identity has no email address")

    existing_email = await session.scalar(select(User).where(User.email == email))
    if existing_email is not None:
        if existing_email.org_id != organization.id:
            # The same answer as below, so a sign-up can't reveal which organization has an email.
            raise HTTPException(status_code=409, detail="Email is already registered")
        if existing_email.auth_subject is not None:
            raise HTTPException(status_code=409, detail="Email is already registered")
        # A pending profile can carry a role and history, so taking it over needs proof of email.
        verified = claims.get("email_verified", claims.get("emailVerified"))
        if verified is not True:
            raise HTTPException(
                status_code=403, detail="Verify your email before activating access"
            )
        existing_email.auth_subject = subject
        await session.commit()
        return {
            "status": "linked",
            "organization": organization.slug,
            "role": existing_email.role,
        }

    user = User(
        org_id=organization.id,
        auth_subject=subject,
        email=email,
        role=UserRole.CUSTOMER,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from exc
    return {"status": "created", "organization": organization.slug, "role": user.role}


@router.get("/api/me")
async def me(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    organization = await session.get(Organization, context.org_id)
    return {"organization": organization.slug, "role": context.role}
