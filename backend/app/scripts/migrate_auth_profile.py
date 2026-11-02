import asyncio

from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import create_engine


async def migrate() -> None:
    """Idempotent transition from local passwords to managed Neon Auth identities."""
    engine = create_engine(get_settings().migration_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_subject VARCHAR(255)")
            )
            await connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_auth_subject "
                    "ON users (auth_subject)"
                )
            )
    finally:
        await engine.dispose()
    print("Managed Auth profile schema is ready.")


if __name__ == "__main__":
    asyncio.run(migrate())
