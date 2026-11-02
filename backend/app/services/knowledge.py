"""Knowledge-base chunking, local embeddings and tenant-scoped vector search."""

import asyncio
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import RequestContext
from app.db.models import KnowledgeChunk, Organization

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# BGE models retrieve better when the query, but not the passage, carries this instruction.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
MAX_CHUNK_CHARS = 1200
# Measured on the seed data: answers score 0.72+, questions it can't answer score 0.57 or less.
MIN_RELEVANCE = 0.65


@dataclass(frozen=True, slots=True)
class Chunk:
    document: str
    source_path: str
    section: str
    content: str

    @property
    def embedding_text(self) -> str:
        # The title path gives short sections ("Opened tea") the context they lack alone.
        return f"{self.document} > {self.section}\n\n{self.content}"


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    document: str
    source_path: str
    section: str
    content: str
    score: float


def _split_long(body: str) -> list[str]:
    """Pack whole paragraphs into chunks of at most MAX_CHUNK_CHARS."""
    parts: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", body):
        if current and len(current) + len(paragraph) + 2 > MAX_CHUNK_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)
    return parts


def chunk_markdown(text: str, source_path: str) -> list[Chunk]:
    """One chunk per `##` section, so each FAQ answer or policy clause is retrieved whole."""
    title_match = re.search(r"^# (.+)$", text, flags=re.MULTILINE)
    document = title_match.group(1).strip() if title_match else Path(source_path).stem
    body = text[title_match.end() :] if title_match else text

    chunks: list[Chunk] = []
    for index, block in enumerate(re.split(r"^## ", body, flags=re.MULTILINE)):
        if index == 0:
            section, content = "Overview", block.strip()
        else:
            heading, _, rest = block.partition("\n")
            section, content = heading.strip(), rest.strip()
        if not content:
            continue
        parts = _split_long(content)
        for number, part in enumerate(parts, start=1):
            label = section if len(parts) == 1 else f"{section} (part {number})"
            chunks.append(Chunk(document, source_path, label, part))
    return chunks


@lru_cache
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBEDDING_MODEL)


def embed_passages(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().embed(texts)]


def embed_query(text: str) -> list[float]:
    return next(iter(_model().embed([QUERY_INSTRUCTION + text]))).tolist()


async def rebuild_org_knowledge(session: AsyncSession, org: Organization, kb_dir: Path) -> int:
    """Replace one organisation's chunks with a fresh embedding of its kb/ documents."""
    chunks = [
        chunk
        for path in sorted(kb_dir.glob("*.md"))
        for chunk in chunk_markdown(
            path.read_text(encoding="utf-8"), f"{kb_dir.parent.name}/kb/{path.name}"
        )
    ]
    vectors = await asyncio.to_thread(embed_passages, [chunk.embedding_text for chunk in chunks])
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.org_id == org.id))
    session.add_all(
        KnowledgeChunk(
            org_id=org.id,
            document=chunk.document,
            source_path=chunk.source_path,
            section=chunk.section,
            content=chunk.content,
            embedding=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    )
    return len(chunks)


def search_statement(org_id: uuid.UUID, query_vector: list[float], limit: int) -> Select:
    """The org filter is part of the query itself, never applied to results afterwards."""
    distance = KnowledgeChunk.embedding.cosine_distance(query_vector).label("distance")
    return (
        select(KnowledgeChunk, distance)
        .where(KnowledgeChunk.org_id == org_id)
        .order_by(distance)
        .limit(limit)
    )


async def search_knowledge(
    session: AsyncSession,
    context: RequestContext,
    query: str,
    limit: int = 4,
    min_score: float = MIN_RELEVANCE,
) -> list[KnowledgeHit]:
    query_vector = await asyncio.to_thread(embed_query, query)
    rows = await session.execute(search_statement(context.org_id, query_vector, limit))
    return [
        KnowledgeHit(
            document=chunk.document,
            source_path=chunk.source_path,
            section=chunk.section,
            content=chunk.content,
            score=round(1 - distance, 4),
        )
        for chunk, distance in rows
        if 1 - distance >= min_score
    ]
