from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import rate_limited_context
from app.core.security import RequestContext
from app.db.database import get_session
from app.services.knowledge import search_knowledge

router = APIRouter()


@router.get("/api/knowledge/search")
async def knowledge_search(
    q: str,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    hits = await search_knowledge(session, context, q)
    return [asdict(hit) for hit in hits]
