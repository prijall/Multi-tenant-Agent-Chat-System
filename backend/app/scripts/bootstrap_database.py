import asyncio

from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import create_engine
from app.db.models import Base

EXTENSIONS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
]

# Columns added after the first deploy; create_all only creates missing tables.
COLUMN_UPGRADES = [
    "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) "
    "NOT NULL DEFAULT 'Asia/Kathmandu'",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR(40) "
    "NOT NULL DEFAULT 'cash_on_delivery'",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS late_fee NUMERIC(12, 2) NOT NULL DEFAULT 0",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS cards JSON NOT NULL DEFAULT '[]'",
    "ALTER TABLE pending_actions ADD COLUMN IF NOT EXISTS message_id UUID "
    "REFERENCES chat_messages (id) ON DELETE SET NULL",
]

# One login identity maps to exactly one application organization. The functional
# index also prevents case variants from being invited into different tenants.
INDEX_UPGRADES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email))",
]

# Two customers grabbing the same slot race to insert; Postgres rejects the loser
# atomically, so no application-level lock is needed. Enum values are stored by name.
EXCLUSION_CONSTRAINTS = {
    "ex_booking_practitioner_overlap": (
        "EXCLUDE USING gist (practitioner_id WITH =, tstzrange(starts_at, ends_at) WITH &&) "
        "WHERE (status = 'CONFIRMED')"
    ),
    "ex_booking_customer_overlap": (
        "EXCLUDE USING gist (customer_id WITH =, tstzrange(starts_at, ends_at) WITH &&) "
        "WHERE (status = 'CONFIRMED')"
    ),
}


async def bootstrap() -> None:
    """Create the schema through Neon's direct, unpooled connection."""
    engine = create_engine(get_settings().migration_database_url)
    try:
        async with engine.begin() as connection:
            for statement in EXTENSIONS:
                await connection.execute(text(statement))
            await connection.run_sync(Base.metadata.create_all)
            for statement in COLUMN_UPGRADES:
                await connection.execute(text(statement))
            for statement in INDEX_UPGRADES:
                await connection.execute(text(statement))
            for name, definition in EXCLUSION_CONSTRAINTS.items():
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}
                )
                if not exists:
                    await connection.execute(
                        text(f"ALTER TABLE bookings ADD CONSTRAINT {name} {definition}")
                    )
    finally:
        await engine.dispose()
    print("Database schema is ready.")


if __name__ == "__main__":
    asyncio.run(bootstrap())
