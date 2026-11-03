"""What a scenario expects, as steps that fail with a plain reason.

The database is the main judge. Tool activity comes next, and reply text last, with loose matches
so that a model rewording a correct answer doesn't count as a failure.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.models import (
    Booking,
    BookingStatus,
    Order,
    OrderStatus,
    PendingAction,
    PendingActionStatus,
    Product,
)
from evals.harness import KATHMANDU, CheckFailed, Step, World


def expect(condition: object, reason: str) -> None:
    if not condition:
        raise CheckFailed(reason)


def _normalized(text: str) -> str:
    """Models vary between straight and curly apostrophes, and that isn't a change of meaning."""
    return text.replace("’", "'").lower()


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    normalized = _normalized(text)
    return any(_normalized(word) in normalized for word in words)


def reply_mentions(*any_of: str) -> Step:
    async def check(world: World) -> None:
        text = world.last_reply.content
        expect(_has_any(text, any_of), f"Reply mentions none of {any_of}: {text[:200]!r}")

    return check


def reply_avoids(*words: str) -> Step:
    async def check(world: World) -> None:
        text = world.last_reply.content
        for word in words:
            expect(not _has_any(text, (word,)), f"Reply leaks {word!r}: {text[:200]!r}")

    return check


def cited(document_part: str) -> Step:
    async def check(world: World) -> None:
        citations = world.last_reply.citations
        expect(
            any(document_part.lower() in item["document"].lower() for item in citations),
            f"No citation from a {document_part!r} document: {citations}",
        )

    return check


async def no_citations(world: World) -> None:
    citations = [f"{item['document']} > {item['section']}" for item in world.last_reply.citations]
    expect(not citations, f"Unrelated citations shown: {citations}")


def tool_ran(name: str, status: str | None = None) -> Step:
    async def check(world: World) -> None:
        runs = [run for run in await world.tool_runs() if run.tool_name == name]
        expect(runs, f"The agent never called {name}")
        if status:
            statuses = [run.status for run in runs]
            expect(status in statuses, f"{name} was {statuses}, expected {status}")

    return check


def card_is(action_type: str) -> Step:
    async def check(world: World) -> None:
        actions = await pending(world)
        expect(actions, f"Expected a {action_type} Confirm card, but there is none")
        expect(
            actions[-1].action_type == action_type,
            f"Confirm card is {actions[-1].action_type}, expected {action_type}",
        )

    return check


async def pending(world: World) -> list[PendingAction]:
    return await world.rows(
        select(PendingAction)
        .where(
            PendingAction.conversation_id == world.conversation_id,
            PendingAction.status == PendingActionStatus.PENDING,
        )
        .order_by(PendingAction.created_at)
    )


async def no_card(world: World) -> None:
    actions = await pending(world)
    expect(not actions, f"Unexpected Confirm card: {[a.action_type for a in actions]}")


def card_total(total: str) -> Step:
    async def check(world: World) -> None:
        actions = await pending(world)
        expect(actions, "Expected a Confirm card, but there is none")
        shown = actions[-1].payload["summary"].get("total")
        expect(shown == total, f"Confirm card total is {shown}, expected {total}")

    return check


def card_note_mentions(*any_of: str) -> Step:
    async def check(world: World) -> None:
        actions = await pending(world)
        expect(actions, "Expected a Confirm card, but there is none")
        note = str(actions[-1].payload["summary"].get("note", ""))
        expect(_has_any(note, any_of), f"Card note mentions none of {any_of}: {note!r}")

    return check


def card_time(day: date, hour: int) -> Step:
    async def check(world: World) -> None:
        actions = await pending(world)
        expect(actions, "Expected a Confirm card, but there is none")
        value = actions[-1].payload["params"]["starts_at"]

        local = datetime.fromisoformat(value).astimezone(KATHMANDU)
        expect(
            local.date() == day and local.hour == hour,
            f"Confirm card is for {local:%Y-%m-%d %H:%M}, expected {day} {hour:02d}:00",
        )

    return check


def orders_are(count: int) -> Step:
    async def check(world: World) -> None:
        actual = await world.order_count()
        expect(actual == count, f"Customer has {actual} orders, expected {count}")

    return check


def remember_orders(key: str = "orders") -> Step:
    async def step(world: World) -> None:
        world.values[key] = await world.order_count()

    return step


def orders_grew_by(count: int, key: str = "orders") -> Step:
    async def check(world: World) -> None:
        actual = await world.order_count() - world.values[key]
        expect(actual == count, f"Orders changed by {actual}, expected {count}")

    return check


def stock_is(sku: str, stock: int) -> Step:
    async def check(world: World) -> None:
        actual = await world.scalar(
            select(Product.stock).where(Product.org_id == world.org_id, Product.sku == sku)
        )
        expect(actual == stock, f"{sku} stock is {actual}, expected {stock}")

    return check


async def newest_order(world: World) -> Order | None:
    return await world.scalar(
        select(Order)
        .where(Order.customer_id == world.user_id)
        .order_by(Order.created_at.desc())
        .limit(1)
    )


def newest_order_has(
    *, total: str | None = None, address: str | None = None, status: OrderStatus | None = None
) -> Step:
    async def check(world: World) -> None:
        order = await newest_order(world)
        expect(order, "Customer has no orders")
        if total is not None:
            expect(order.total == Decimal(total), f"Order total is {order.total}, expected {total}")
        if address is not None:
            expect(
                address.lower() in order.delivery_address.lower(),
                f"Order address is {order.delivery_address!r}, expected {address!r}",
            )
        if status is not None:
            expect(
                order.status is status, f"Order is {order.status.value}, expected {status.value}"
            )

    return check


def order_in_values_has(key: str, status: OrderStatus) -> Step:
    async def check(world: World) -> None:
        actual = await world.scalar(select(Order.status).where(Order.id == world.values[key]))
        expect(actual is status, f"Order is {actual.value}, expected {status.value}")

    return check


async def customer_bookings(world: World) -> list[Booking]:
    return await world.rows(
        select(Booking).where(
            Booking.customer_id == world.user_id, Booking.status == BookingStatus.CONFIRMED
        )
    )


def confirmed_bookings_are(count: int) -> Step:
    async def check(world: World) -> None:
        actual = len(await customer_bookings(world))
        expect(actual == count, f"Customer has {actual} confirmed bookings, expected {count}")

    return check


def newest_booking_at(day: date | str, hour: int) -> Step:
    async def check(world: World) -> None:
        expected = world.values[day] if isinstance(day, str) else day
        bookings = await customer_bookings(world)
        expect(bookings, "Customer has no confirmed bookings")
        starts = [booking.starts_at.astimezone(KATHMANDU) for booking in bookings]
        expect(
            any(start.date() == expected and start.hour == hour for start in starts),
            f"Bookings are at {[f'{s:%Y-%m-%d %H:%M}' for s in starts]}, expected {expected} "
            f"{hour:02d}:00",
        )

    return check


def booking_unchanged(key: str) -> Step:
    """Compares against the id and start time a setup step stored under this key."""

    async def check(world: World) -> None:
        booking_id, start = world.values[key]
        booking = await world.scalar(select(Booking).where(Booking.id == booking_id))
        expect(booking.status is BookingStatus.CONFIRMED, f"Booking is {booking.status.value}")
        expect(booking.starts_at == start, f"Booking moved from {start} to {booking.starts_at}")

    return check


def booking_cancelled_with_fee(key: str, fee: str) -> Step:
    async def check(world: World) -> None:
        booking_id, _ = world.values[key]
        booking = await world.scalar(select(Booking).where(Booking.id == booking_id))
        expect(booking.status is BookingStatus.CANCELLED, f"Booking is {booking.status.value}")
        expect(booking.late_fee == Decimal(fee), f"Fee is {booking.late_fee}, expected {fee}")

    return check


def slot_search_for(day_key: str) -> Step:
    async def check(world: World) -> None:
        runs = [run for run in await world.tool_runs() if run.tool_name == "find_available_slots"]
        expect(runs, "The agent never searched for slots")
        expected = world.values[day_key].isoformat()
        searched = [run.input_data.get("date_from") for run in runs]
        expect(expected in searched, f"Searched from {searched}, expected {expected}")

    return check
