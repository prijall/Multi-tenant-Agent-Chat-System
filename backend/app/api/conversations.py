import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.actions import (
    ActionError,
    action_response,
    actions_by_message,
    choose_slot,
    confirm_action,
    dismiss_action,
)
from app.agents.answer_cache import AnswerCache, get_answer_cache
from app.agents.contracts import AgentProvider
from app.agents.providers import build_provider, build_suggestion_provider
from app.agents.runtime import owned_conversation, stream_agent
from app.agents.suggestions import suggest_next_message
from app.api.dependencies import get_request_context, rate_limited_context
from app.api.schemas import ChatInput, ConversationInput, SlotChoiceInput
from app.core.config import get_settings
from app.core.rate_limit import suggestion_limiter
from app.core.security import RequestContext
from app.db.database import get_session, get_session_factory
from app.db.models import ChatMessage, Conversation, PendingAction

logger = logging.getLogger(__name__)

router = APIRouter()


def conversation_response(conversation: Conversation) -> dict[str, object]:
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def message_response(
    message: ChatMessage, actions: list[dict[str, Any]] | None = None
) -> dict[str, object]:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "citations": message.citations,
        "cards": message.cards or [],
        "created_at": message.created_at,
        "actions": actions or [],
    }


@router.post("/api/conversations", status_code=201)
async def create_conversation(
    payload: ConversationInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = Conversation(
        org_id=context.org_id,
        user_id=context.user_id,
        title=payload.title.strip(),
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation_response(conversation)


@router.get("/api/conversations")
async def conversations(
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = await session.scalars(
        select(Conversation)
        .where(
            Conversation.org_id == context.org_id,
            Conversation.user_id == context.user_id,
        )
        .order_by(Conversation.updated_at.desc())
    )
    return [conversation_response(row) for row in rows]


@router.patch("/api/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationInput,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    conversation.title = payload.title.strip()
    await session.commit()
    return conversation_response(conversation)


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    conversation = await owned_conversation(session, context, conversation_id)
    await session.delete(conversation)
    await session.commit()
    return {"status": "deleted"}


@router.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    conversation = await owned_conversation(session, context, conversation_id)
    rows = await session.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    actions = await actions_by_message(session, conversation.id)
    return [message_response(row, actions.get(row.id)) for row in rows]


async def resolve_action(
    resolver: Callable[..., Awaitable[tuple[ChatMessage, PendingAction]]],
    session: AsyncSession,
    context: RequestContext,
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    try:
        message, action = await resolver(session, context, conversation, action_id)
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"message": message_response(message), "action": action_response(action)}


@router.post("/api/conversations/{conversation_id}/actions/{action_id}/confirm")
async def confirm_conversation_action(
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """The only way a chat proposal takes effect: the customer's own request, never the model's."""
    return await resolve_action(confirm_action, session, context, conversation_id, action_id)


@router.post("/api/conversations/{conversation_id}/actions/{action_id}/dismiss")
async def dismiss_conversation_action(
    conversation_id: uuid.UUID,
    action_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    return await resolve_action(dismiss_action, session, context, conversation_id, action_id)


@router.post("/api/conversations/{conversation_id}/slot-choice")
async def choose_conversation_slot(
    conversation_id: uuid.UUID,
    payload: SlotChoiceInput,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    conversation = await owned_conversation(session, context, conversation_id)
    question, reply = await choose_slot(
        session,
        context,
        conversation,
        payload.service_id,
        payload.practitioner_id,
        payload.starts_at,
        payload.booking_id,
    )
    actions = await actions_by_message(session, conversation_id)
    return {
        "question": message_response(question),
        "message": message_response(reply, actions.get(reply.id)),
    }


def get_agent_provider() -> AgentProvider:
    provider = build_provider(get_settings())
    if provider is None:
        raise HTTPException(status_code=503, detail="The agent's LLM is not configured")
    return provider


def server_sent_event(event: dict[str, object]) -> str:
    return f"data: {json.dumps(jsonable_encoder(event))}\n\n"


async def agent_events(
    session_factory: async_sessionmaker[AsyncSession],
    context: RequestContext,
    conversation_id: uuid.UUID,
    content: str,
    provider: AgentProvider,
    answer_cache: AnswerCache | None,
) -> AsyncIterator[str]:
    """Runs on its own session: a request-scoped one may close before the stream finishes."""
    async with session_factory() as session:
        conversation = await owned_conversation(session, context, conversation_id)
        try:
            async with aclosing(
                stream_agent(
                    session,
                    context,
                    conversation,
                    content,
                    provider,
                    max_tool_rounds=get_settings().agent_max_tool_rounds,
                    answer_cache=answer_cache,
                )
            ) as events:
                async for event in events:
                    if event["type"] == "done":
                        message = event["message"]
                        actions = await actions_by_message(session, conversation_id)
                        event = {
                            "type": "done",
                            "message": message_response(message, actions.get(message.id)),
                        }
                    yield server_sent_event(event)
        except Exception:
            logger.exception("Agent run failed for conversation %s", conversation_id)
            yield server_sent_event(
                {"type": "error", "detail": "The agent could not complete the request"}
            )


def get_suggestion_provider() -> AgentProvider:
    provider = build_suggestion_provider(get_settings())
    if provider is None:
        raise HTTPException(status_code=503, detail="The agent's LLM is not configured")
    return provider


@router.post("/api/conversations/{conversation_id}/suggestion")
async def next_message_suggestion(
    conversation_id: uuid.UUID,
    context: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
    provider: AgentProvider = Depends(get_suggestion_provider),
) -> dict[str, str | None]:
    conversation = await owned_conversation(session, context, conversation_id)
    suggestion_limiter.check(str(context.user_id))
    return {"suggestion": await suggest_next_message(session, conversation, provider)}


@router.post("/api/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    payload: ChatInput,
    context: RequestContext = Depends(rate_limited_context),
    session: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    provider: AgentProvider = Depends(get_agent_provider),
    answer_cache: AnswerCache | None = Depends(get_answer_cache),
) -> StreamingResponse:
    await owned_conversation(session, context, conversation_id)
    await session.rollback()
    return StreamingResponse(
        agent_events(
            session_factory, context, conversation_id, payload.content, provider, answer_cache
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
