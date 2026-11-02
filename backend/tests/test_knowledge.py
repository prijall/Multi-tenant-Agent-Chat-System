import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings
from app.services.knowledge import MAX_CHUNK_CHARS, chunk_markdown, search_statement

FAQ = """# Frequently Asked Questions

## Do you sell tea bags?

No, loose leaf only.

## How long does delivery take?

One to two business days in the valley.
"""


def test_each_faq_question_becomes_one_cited_chunk():
    chunks = chunk_markdown(FAQ, "himali-tea/kb/faq.md")

    assert [(chunk.document, chunk.section) for chunk in chunks] == [
        ("Frequently Asked Questions", "Do you sell tea bags?"),
        ("Frequently Asked Questions", "How long does delivery take?"),
    ]
    assert chunks[0].source_path == "himali-tea/kb/faq.md"
    assert chunks[0].embedding_text.startswith("Frequently Asked Questions > Do you sell tea bags?")


def test_long_sections_split_on_paragraphs_and_text_before_first_heading_is_kept():
    paragraph = "word " * 100  # 500 chars: two fit in one chunk, the third starts another
    text = f"# Guide\n\nIntro line.\n\n## Long\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}"

    chunks = chunk_markdown(text, "guide.md")

    assert chunks[0].section == "Overview"
    assert [chunk.section for chunk in chunks[1:]] == ["Long (part 1)", "Long (part 2)"]
    assert all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)


def test_search_query_filters_by_org_inside_sql():
    org_id = uuid.uuid4()
    compiled = search_statement(org_id, [0.0] * 384, 4).compile(dialect=postgresql.dialect())

    assert "knowledge_chunks.org_id = %(org_id_1)s" in str(compiled)
    assert compiled.params["org_id_1"] == org_id


requires_postgres = pytest.mark.skipif(
    get_settings().database_url.startswith("sqlite"),
    reason="vector search needs the Postgres DATABASE_URL",
)


@requires_postgres
async def test_search_never_returns_another_orgs_chunk_even_for_its_exact_text():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.security import RequestContext
    from app.db.database import create_engine
    from app.db.models import KnowledgeChunk, Organization, UserRole
    from app.services.knowledge import embed_passages, search_knowledge

    engine = create_engine()
    marker = uuid.uuid4().hex[:10]
    secret = "Lotus staff wifi password and internal pricing notes"
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            own = Organization(slug=f"test-own-{marker}", name="Own")
            other = Organization(slug=f"test-other-{marker}", name="Other")
            session.add_all([own, other])
            await session.flush()
            own_vector, secret_vector = embed_passages(["Tea storage advice", secret])
            session.add_all(
                [
                    KnowledgeChunk(
                        org_id=own.id,
                        document="Own",
                        source_path="own.md",
                        section="Storage",
                        content="Tea storage advice",
                        embedding=own_vector,
                    ),
                    KnowledgeChunk(
                        org_id=other.id,
                        document="Other",
                        source_path="other.md",
                        section="Secret",
                        content=secret,
                        embedding=secret_vector,
                    ),
                ]
            )
            await session.flush()

            context = RequestContext(uuid.uuid4(), own.id, UserRole.CUSTOMER)
            hits = await search_knowledge(session, context, secret, limit=10, min_score=-1)

            assert [hit.source_path for hit in hits] == ["own.md"]
            await session.rollback()
    finally:
        await engine.dispose()
