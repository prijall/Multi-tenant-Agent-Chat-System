from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import interactive_limiter
from app.core.security import RequestContext, authenticated_subject, resolve_context_from_session
from app.db.database import get_session


async def get_request_context(
    auth_subject: str = Depends(authenticated_subject),
    session: AsyncSession = Depends(get_session),
) -> RequestContext:
    return await resolve_context_from_session(session, auth_subject)


async def rate_limited_context(
    context: RequestContext = Depends(get_request_context),
) -> RequestContext:
    interactive_limiter.check(str(context.user_id))
    return context
