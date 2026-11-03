"""Reuses knowledge-base answers for reworded questions, without calling the LLM.

Only policy and FAQ answers are stored, per organisation and for one day. Anything from live data
or about a customer always goes to the agent. Embedding similarity alone isn't safe here: "cancel
more than 24 hours ahead" and "cancel within 24 hours" score 0.956, higher than many true
rewordings, yet need opposite answers. So a hit also needs the same top source section, and no
disagreement on words that change the meaning. Any Redis failure just means asking the LLM.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import numpy as np
from redis.asyncio import Redis
from redis.commands.search.field import TagField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from redis.exceptions import ResponseError

from app.core.config import get_settings
from app.db.models import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

INDEX_NAME = "idx:answers"
KEY_PREFIX = "answer:"
MAX_QUESTION_WORDS = 20

# These flip an answer while barely moving the embedding.
MEANING_WORDS = {
    "after", "ahead", "before", "cancel", "damaged", "early", "inside", "international", "late",
    "more", "move", "no", "not", "opened", "outside", "reschedule", "unopened", "valley",
    "within", "without",
}  # fmt: skip
# A shared answer mustn't carry someone's details, or stand in for looking up their own records.
PERSONAL = re.compile(
    r"\b(i'm|i am|am i|my|mine|did you|have you|the (order|booking|appointment))\b"
    r"|\b(order|booking)\s*#?\s*\d|@|\d{5,}",
    re.I,
)


def _words(question: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", question.lower()))


def is_cacheable_question(question: str) -> bool:
    """One short, impersonal question: anything longer may carry instructions or details."""
    text = question.strip()
    sentences = [part for part in re.split(r"[.!?]+", text) if part.strip()]
    return (
        len(sentences) == 1
        and len(text.split()) <= MAX_QUESTION_WORDS
        and not PERSONAL.search(text)
    )


def means_the_same(first: str, second: str) -> bool:
    return not (_words(first) ^ _words(second)) & MEANING_WORDS


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    question: str
    content: str
    citations: list[dict[str, Any]]
    source: str
    similarity: float = 1.0


class AnswerStore(Protocol):
    async def nearest(self, org_id: uuid.UUID, vector: list[float]) -> CachedAnswer | None: ...

    async def save(
        self, org_id: uuid.UUID, vector: list[float], answer: CachedAnswer, ttl_seconds: int
    ) -> None: ...

    async def purge(self, org_id: uuid.UUID) -> None: ...


class RedisAnswerStore:
    """Vector search through the query engine in Redis Stack, which plain Redis images lack."""

    def __init__(self, client: Redis):
        self.client = client
        self._index_ready = False

    async def _ensure_index(self) -> None:
        if self._index_ready:
            return
        index = self.client.ft(INDEX_NAME)
        try:
            await index.info()
        except ResponseError:
            await index.create_index(
                [
                    TagField("org"),
                    VectorField(
                        "embedding",
                        "FLAT",
                        {
                            "TYPE": "FLOAT32",
                            "DIM": EMBEDDING_DIMENSIONS,
                            "DISTANCE_METRIC": "COSINE",
                        },
                    ),
                ],
                definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH),
            )
        self._index_ready = True

    async def nearest(self, org_id: uuid.UUID, vector: list[float]) -> CachedAnswer | None:
        await self._ensure_index()
        # The organisation is part of the search itself, as with the knowledge base.
        query = (
            Query(f"(@org:{{{org_id.hex}}})=>[KNN 1 @embedding $vector AS distance]")
            .sort_by("distance")
            .return_fields("answer", "distance")
            .dialect(2)
        )
        found = await self.client.ft(INDEX_NAME).search(
            query, query_params={"vector": np.asarray(vector, dtype=np.float32).tobytes()}
        )
        if not found.docs:
            return None
        document = found.docs[0]
        return CachedAnswer(**json.loads(document.answer), similarity=1 - float(document.distance))

    async def save(
        self, org_id: uuid.UUID, vector: list[float], answer: CachedAnswer, ttl_seconds: int
    ) -> None:
        await self._ensure_index()
        key = f"{KEY_PREFIX}{org_id.hex}:{uuid.uuid4().hex}"
        payload = {
            "question": answer.question,
            "content": answer.content,
            "citations": answer.citations,
            "source": answer.source,
        }
        await self.client.hset(
            key,
            mapping={
                "org": org_id.hex,
                "embedding": np.asarray(vector, dtype=np.float32).tobytes(),
                "answer": json.dumps(payload),
            },
        )
        await self.client.expire(key, ttl_seconds)

    async def purge(self, org_id: uuid.UUID) -> None:
        async for key in self.client.scan_iter(match=f"{KEY_PREFIX}{org_id.hex}:*"):
            await self.client.delete(key)


class AnswerCache:
    def __init__(self, store: AnswerStore, min_similarity: float, ttl_seconds: int):
        self.store = store
        self.min_similarity = min_similarity
        self.ttl_seconds = ttl_seconds

    async def candidate(
        self, org_id: uuid.UUID, vector: list[float], question: str
    ) -> CachedAnswer | None:
        """The checks that need no database, so a miss goes straight to the model."""
        try:
            cached = await self.store.nearest(org_id, vector)
        except Exception as exc:  # A cache problem must never cost the customer their answer.
            logger.warning("Answer cache lookup failed: %s", type(exc).__name__)
            return None
        if cached is None or cached.similarity < self.min_similarity:
            return None
        return cached if means_the_same(question, cached.question) else None

    async def remember(
        self,
        org_id: uuid.UUID,
        vector: list[float],
        question: str,
        content: str,
        citations: list[dict[str, Any]],
        source: str,
    ) -> None:
        answer = CachedAnswer(question, content, citations, source)
        try:
            await self.store.save(org_id, vector, answer, self.ttl_seconds)
        except Exception as exc:
            logger.warning("Answer cache save failed: %s", type(exc).__name__)

    async def purge(self, org_id: uuid.UUID) -> None:
        try:
            await self.store.purge(org_id)
        except Exception as exc:
            logger.warning("Answer cache purge failed: %s", type(exc).__name__)


@lru_cache
def get_answer_cache() -> AnswerCache | None:
    settings = get_settings()
    if not settings.redis_url:
        return None
    # A slow cache must never be slower than simply asking the model.
    client = Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
    return AnswerCache(
        RedisAnswerStore(client),
        settings.answer_cache_min_similarity,
        settings.answer_cache_ttl_seconds,
    )
