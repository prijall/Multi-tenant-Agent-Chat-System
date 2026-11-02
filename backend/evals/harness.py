"""Runs real conversations through the real agent loop, then judges the database afterwards.

Everything happens in a private Postgres schema that is created for the run and dropped at the
end, so it needs no separate database and can't touch real data. Each scenario starts from a
freshly seeded copy of both organisations.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agents.actions import confirm_action, dismiss_action
from app.agents.contracts import AgentProvider, ProviderTurn
from app.agents.runtime import run_agent
from app.core.security import RequestContext
from app.db.database import asyncpg_url
from app.db.models import (
    Base,
    Booking,
    BookingStatus,
    ChatMessage,
    Conversation,
    Holiday,
    OpeningHours,
    Order,
    Organization,
    PendingAction,
    PendingActionStatus,
    Practitioner,
    Service,
    ToolRun,
    User,
)
from app.scripts.bootstrap_database import EXCLUSION_CONSTRAINTS, INDEX_UPGRADES
from app.scripts.seed import SEED_DIR, seed_customers, seed_products, seed_schedule, upsert
from app.services.knowledge import rebuild_org_knowledge

KATHMANDU = ZoneInfo("Asia/Kathmandu")
RESEEDED_TABLES = [
    table.name
    for table in Base.metadata.sorted_tables
    if table.name not in {"organizations", "knowledge_chunks"}
]


class CheckFailed(Exception):
    """The agent did the wrong thing, as opposed to the run being unable to finish."""


class PacedProvider:
    """Keeps requests under a free tier's per-minute limit, and counts them for the report."""

    def __init__(self, provider: AgentProvider, seconds_per_call: float):
        self.provider = provider
        self.seconds_per_call = seconds_per_call
        self.calls = 0
        self._last = 0.0

    async def _wait(self) -> None:
        delay = self._last + self.seconds_per_call - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()
        self.calls += 1

    async def complete(self, **arguments: Any) -> ProviderTurn:
        await self._wait()
        return await self.provider.complete(**arguments)

    async def stream(self, **arguments: Any):
        await self._wait()
        async for item in self.provider.stream(**arguments):
            yield item


class EvalDatabase:
    def __init__(self, database_url: str):
        self.schema = f"eval_{uuid.uuid4().hex[:12]}"
        url, connect_args = asyncpg_url(database_url)
        self.engine: AsyncEngine = create_async_engine(
            url, pool_pre_ping=True, connect_args=connect_args
        )

        # Set per connection, because Neon's proxy can drop this from the startup settings.
        # Extensions such as vector live in public; every table lives in the schema.
        @event.listens_for(self.engine.sync_engine, "connect")
        def use_private_schema(connection, _record) -> None:
            connection.run_async(
                lambda raw: raw.execute(f"SET search_path TO {self.schema}, public")
            )

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.seed_data = {
            path.name: json.loads((path / "org.json").read_text(encoding="utf-8"))
            for path in sorted(SEED_DIR.iterdir())
            if path.is_dir()
        }

    async def create(self, *, with_knowledge: bool = True) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {self.schema}"))
            # Without this, tables that exist in public would count as already created.
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, checkfirst=False))
            for statement in INDEX_UPGRADES:
                await connection.execute(text(statement))
            for name, definition in EXCLUSION_CONSTRAINTS.items():
                await connection.execute(
                    text(f"ALTER TABLE bookings ADD CONSTRAINT {name} {definition}")
                )
        async with self.sessions() as session:
            for slug, data in self.seed_data.items():
                org = await upsert(
                    session,
                    Organization,
                    {"slug": slug},
                    {"name": data["name"], "timezone": data.get("timezone", "Asia/Kathmandu")},
                )
                await session.flush()
                if with_knowledge:
                    await rebuild_org_knowledge(session, org, SEED_DIR / slug / "kb")
            await session.commit()

    async def reset(self) -> None:
        """A fresh start for one scenario; embeddings are kept because rebuilding them is slow."""
        async with self.sessions() as session:
            names = ", ".join(RESEEDED_TABLES)
            await session.execute(text(f"TRUNCATE {names} CASCADE"))
            for slug, data in self.seed_data.items():
                org = await session.scalar(select(Organization).where(Organization.slug == slug))
                await seed_products(session, org, data.get("products", []))
                await seed_schedule(session, org, data)
                await session.flush()
                await seed_customers(session, org, data.get("customers", []))
            await session.commit()

    async def drop(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE"))
        await self.engine.dispose()


@dataclass
class World:
    """One scenario's customer and conversation, with what's needed to act and to judge."""

    sessions: async_sessionmaker[AsyncSession]
    provider: AgentProvider
    org_id: uuid.UUID
    user_id: uuid.UUID
    context: RequestContext
    conversation_id: uuid.UUID
    values: dict[str, Any] = field(default_factory=dict)
    replies: list[ChatMessage] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last_reply(self) -> ChatMessage:
        if not self.replies:
            raise CheckFailed("The agent has not replied yet")
        return self.replies[-1]

    async def say(self, content: str) -> None:
        async with self.sessions() as session:
            conversation = await session.get(Conversation, self.conversation_id)
            reply = await run_agent(session, self.context, conversation, content, self.provider)
        self.replies.append(reply)
        self.transcript.append({"customer": content, "agent": reply.content})

    async def _latest_action(self, session: AsyncSession) -> PendingAction:
        action = await session.scalar(
            select(PendingAction)
            .where(
                PendingAction.conversation_id == self.conversation_id,
                PendingAction.status == PendingActionStatus.PENDING,
            )
            .order_by(PendingAction.created_at.desc())
            .limit(1)
        )
        if action is None:
            raise CheckFailed("There is no Confirm card to press")
        return action

    async def press(self, resolver: Callable[..., Awaitable[Any]], label: str) -> None:
        async with self.sessions() as session:
            action = await self._latest_action(session)
            conversation = await session.get(Conversation, self.conversation_id)
            reply, _ = await resolver(session, self.context, conversation, action.id)
        self.replies.append(reply)
        self.transcript.append({"customer": f"[pressed {label}]", "agent": reply.content})

    async def scalar(self, statement: Any) -> Any:
        async with self.sessions() as session:
            return await session.scalar(statement)

    async def rows(self, statement: Any) -> list[Any]:
        async with self.sessions() as session:
            return list(await session.scalars(statement))

    async def tool_runs(self) -> list[ToolRun]:
        return await self.rows(
            select(ToolRun)
            .where(ToolRun.conversation_id == self.conversation_id)
            .order_by(ToolRun.created_at)
        )

    async def order_count(self) -> int:
        return await self.scalar(
            select(func.count(Order.id)).where(Order.customer_id == self.user_id)
        )

    async def customer(self, email: str) -> User:
        return await self.scalar(select(User).where(User.email == email))

    async def add_booking(
        self, email: str, service_code: str, practitioner_code: str, starts_at: datetime
    ) -> Booking:
        """Put an appointment directly in the database, as scenery for the scenario."""
        async with self.sessions() as session:
            customer = await session.scalar(select(User).where(User.email == email))
            service = await session.scalar(
                select(Service).where(Service.org_id == self.org_id, Service.code == service_code)
            )
            practitioner = await session.scalar(
                select(Practitioner).where(
                    Practitioner.org_id == self.org_id, Practitioner.code == practitioner_code
                )
            )
            booking = Booking(
                org_id=self.org_id,
                customer_id=customer.id,
                service_id=service.id,
                practitioner_id=practitioner.id,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=service.duration_minutes),
                status=BookingStatus.CONFIRMED,
                price=service.price,
            )
            session.add(booking)
            await session.commit()
            return booking

    async def open_day(self, *, weekdays: set[int] | None = None, after: int = 3) -> date:
        """The first date at least `after` days away that the studio is open and on a weekday."""
        holidays = set(await self.rows(select(Holiday.day).where(Holiday.org_id == self.org_id)))
        open_days = set(
            await self.rows(select(OpeningHours.weekday).where(OpeningHours.org_id == self.org_id))
        )
        day = datetime.now(KATHMANDU).date() + timedelta(days=after)
        while (
            day in holidays
            or day.weekday() not in open_days
            or (weekdays is not None and day.weekday() not in weekdays)
        ):
            day += timedelta(days=1)
        return day

    async def next_holiday(self) -> date:
        today = datetime.now(KATHMANDU).date()
        return await self.scalar(
            select(Holiday.day)
            .where(Holiday.org_id == self.org_id, Holiday.day > today)
            .order_by(Holiday.day)
            .limit(1)
        )


def spoken(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def local(day: date, hour: int) -> datetime:
    return datetime.combine(day, datetime.min.time().replace(hour=hour), KATHMANDU)


Step = Callable[[World], Awaitable[None]]


def say(message: str | Callable[[World], str]) -> Step:
    async def step(world: World) -> None:
        await world.say(message(world) if callable(message) else message)

    return step


async def confirm(world: World) -> None:
    await world.press(confirm_action, "Confirm")


async def dismiss(world: World) -> None:
    await world.press(dismiss_action, "Not now")


@dataclass(frozen=True)
class Scenario:
    name: str
    org: str
    customer: str
    covers: str
    steps: list[Step]
    setup: Step | None = None


@dataclass
class Result:
    scenario: Scenario
    status: str
    detail: str = ""
    seconds: float = 0
    model_calls: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.scenario.name,
            "org": self.scenario.org,
            "covers": self.scenario.covers,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 1),
            "model_calls": self.model_calls,
            "transcript": self.transcript,
            "tools": self.tools,
        }


async def run_scenario(
    database: EvalDatabase, scenario: Scenario, provider: PacedProvider
) -> Result:
    await database.reset()
    started = time.monotonic()
    calls_before = provider.calls
    async with database.sessions() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == scenario.org))
        user = await session.scalar(select(User).where(User.email == scenario.customer))
        conversation = Conversation(org_id=org.id, user_id=user.id, title=scenario.name)
        session.add(conversation)
        await session.commit()
        world = World(
            sessions=database.sessions,
            provider=provider,
            org_id=org.id,
            user_id=user.id,
            context=RequestContext(user.id, org.id, user.role),
            conversation_id=conversation.id,
        )
    status, detail = "PASS", ""
    try:
        if scenario.setup:
            await scenario.setup(world)
        for step in scenario.steps:
            await step(world)
    except CheckFailed as failure:
        status, detail = "FAIL", str(failure)
    except Exception as exc:
        # An outage or quota says nothing about the agent, so it mustn't look like its failure.
        status, detail = "ERROR", f"{type(exc).__name__}: {str(exc)[:300]}"
    runs = await world.tool_runs()
    return Result(
        scenario=scenario,
        status=status,
        detail=detail,
        seconds=time.monotonic() - started,
        model_calls=provider.calls - calls_before,
        transcript=world.transcript,
        tools=[
            {
                "tool": run.tool_name,
                "status": run.status,
                "input": run.input_data,
                "error": run.output_data.get("error"),
            }
            for run in runs
        ],
    )
