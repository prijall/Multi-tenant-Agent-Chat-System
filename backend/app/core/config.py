from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", BACKEND_DIR / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./local.db"
    database_direct_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL_UNPOOLED", "DATABASE_DIRECT_URL"),
    )
    frontend_origin: str = "http://localhost:3000"
    neon_auth_base_url: str | None = None
    neon_auth_jwks_url: str | None = None
    llm_provider: str = "gemini"
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_fallback_provider: str | None = None
    llm_fallback_model: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_after_seconds: float = 10
    agent_max_tool_rounds: int = 5
    demo_password: str | None = None
    redis_url: str | None = None
    answer_cache_min_similarity: float = 0.90
    answer_cache_ttl_seconds: int = 86400

    @property
    def migration_database_url(self) -> str:
        return self.database_direct_url or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()

