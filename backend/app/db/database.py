from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def asyncpg_url(database_url: str) -> tuple[str, dict[str, object]]:
    """Adapt Neon/libpq URLs for SQLAlchemy's asyncpg dialect without logging secrets."""
    if database_url.startswith("sqlite"):
        return database_url, {}

    parts = urlsplit(database_url)
    if parts.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError("DATABASE_URL must be PostgreSQL or SQLite")

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"sslmode", "channel_binding"}
    ]
    adapted = urlunsplit(
        ("postgresql+asyncpg", parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    return adapted, {"ssl": True}


def create_engine(database_url: str | None = None) -> AsyncEngine:
    raw_url = database_url or get_settings().database_url
    url, connect_args = asyncpg_url(raw_url)
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs.pop("pool_pre_ping")
    elif connect_args:
        kwargs["connect_args"] = connect_args
    return create_async_engine(url, **kwargs)


engine = create_engine()
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """For streaming responses, which outlive a request-scoped session."""
    return SessionFactory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

