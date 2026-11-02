from app.core.config import Settings
from app.db.database import asyncpg_url


def test_neon_url_is_adapted_for_asyncpg_without_libpq_only_options():
    raw = (
        "postgresql://user:secret@example-pooler.neon.tech/db"
        "?sslmode=require&channel_binding=require&application_name=test"
    )

    url, connect_args = asyncpg_url(raw)

    assert url == (
        "postgresql+asyncpg://user:secret@example-pooler.neon.tech/db?application_name=test"
    )
    assert connect_args == {"ssl": True}


def test_neon_unpooled_variable_is_used_for_migrations(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pooled/db")
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://direct/db")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql://pooled/db"
    assert settings.migration_database_url == "postgresql://direct/db"
