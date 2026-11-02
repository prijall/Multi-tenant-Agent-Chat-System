"""Rich cards shown under a reply, built from tool results rather than the model's words."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

MAX_SLOTS_SHOWN = 12
MAX_ROWS_SHOWN = 5


def _local(value: str, timezone: str) -> str:
    moment = datetime.fromisoformat(value)
    # Stored times are always UTC, though SQLite drops the zone on the way back.
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(timezone)).strftime("%a %d %b, %H:%M")


def slots_card(
    slots: list[dict[str, Any]],
    service_id: str,
    booking_id: str | None = None,
    title: str = "Pick a time",
) -> dict[str, Any]:
    """Each slot can be chosen directly; booking_id means choosing one moves that booking."""
    return {
        "type": "slots",
        "title": title,
        "service_id": service_id,
        "booking_id": booking_id,
        "slots": [
            {
                "label": datetime.fromisoformat(str(slot["local_start"])).strftime(
                    "%a %d %b, %H:%M"
                ),
                "practitioner": slot["practitioner"],
                "practitioner_id": slot["practitioner_id"],
                "starts_at": slot["local_start"],
            }
            for slot in slots[:MAX_SLOTS_SHOWN]
        ],
    }


def order_card(detail: dict[str, Any], timezone: str) -> dict[str, Any]:
    return {
        "type": "status",
        "title": f"Order {str(detail['id'])[:8]}",
        "status": detail["status"],
        "rows": [
            {"label": f"{item['quantity']} × {item['name']}", "value": f"Rs {item['unit_price']}"}
            for item in detail["items"]
        ],
        "total": f"Rs {detail['total']}",
        "timeline": [
            {"status": event["status"], "at": _local(event["at"], timezone)}
            for event in detail["history"]
        ],
    }


def orders_card(details: list[dict[str, Any]], timezone: str) -> dict[str, Any]:
    return {
        "type": "status",
        "title": "Your recent orders",
        "rows": [
            {
                "label": f"Rs {detail['total']}",
                "value": _local(detail["created_at"], timezone),
                "status": detail["status"],
            }
            for detail in details[:MAX_ROWS_SHOWN]
        ],
    }


def bookings_card(bookings: list[dict[str, Any]], timezone: str) -> dict[str, Any]:
    """Upcoming appointments come first, because that's what people are asking about."""
    ordered = sorted(bookings, key=lambda booking: booking["status"] != "confirmed")
    return {
        "type": "status",
        "title": "Your bookings",
        "rows": [
            {
                "label": booking["service"],
                "value": f"{_local(booking['local_start'], timezone)} · {booking['practitioner']}",
                "status": booking["status"],
            }
            for booking in ordered[:MAX_ROWS_SHOWN]
        ],
    }


def booking_card(
    service: str, practitioner: str, when: str, status: str, late_fee: Any = None
) -> dict[str, Any]:
    rows = [
        {"label": "Service", "value": service},
        {"label": "Practitioner", "value": practitioner},
        {"label": "When", "value": when},
    ]
    if late_fee:
        rows.append({"label": "Late fee", "value": f"Rs {late_fee}"})
    return {"type": "status", "title": "Your booking", "status": status, "rows": rows}
