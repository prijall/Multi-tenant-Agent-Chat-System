import uuid
from asyncio import to_thread
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import User, UserRole


@dataclass(frozen=True, slots=True)
class RequestContext:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: UserRole


bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def _jwks_client(jwks_url: str) -> jwt.PyJWKClient:
    """One client per JWKS URL so its key cache survives across requests."""
    return jwt.PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)


def _token_issuer(auth_base_url: str) -> str:
    """Neon Auth signs tokens with the origin of its URL, without the /<db>/auth path."""
    parts = urlsplit(auth_base_url)
    return f"{parts.scheme}://{parts.netloc}"


def _verify_token_sync(token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.neon_auth_jwks_url or not settings.neon_auth_base_url:
        raise RuntimeError("Neon Auth environment is not configured")

    signing_key = _jwks_client(settings.neon_auth_jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["EdDSA"],
        issuer=_token_issuer(settings.neon_auth_base_url),
        options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
    )


async def authenticated_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        claims = await to_thread(_verify_token_sync, credentials.credentials)
    except (jwt.PyJWTError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    return claims


async def authenticated_subject(
    claims: Annotated[dict[str, Any], Depends(authenticated_claims)],
) -> str:
    return str(claims["sub"])


async def resolve_context_from_session(
    session: AsyncSession,
    auth_subject: str,
) -> RequestContext:
    """Resolve authorization from app data, never from request tenant fields or claims."""
    user = await session.scalar(select(User).where(User.auth_subject == auth_subject))
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is not provisioned")
    return RequestContext(user_id=user.id, org_id=user.org_id, role=user.role)


def require_admin(context: RequestContext) -> None:
    if context.role is not UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

