"""Run the deterministic suite and print the required business coverage checklist."""

import subprocess
import sys

STORE_SCENARIOS = [
    "tenant catalog isolation",
    "customer order ownership",
    "unknown SKU rejection",
    "insufficient stock rejection",
    "quote total revalidation",
    "stock decrement on confirmed order",
    "pending-order cancellation restores stock",
    "non-pending order protection",
    "cross-organization order lookup rejection",
    "order status transition validation",
    "reorder rechecks current stock and price",
    "public signup cannot choose an admin role",
]

STUDIO_SCENARIOS = [
    "tenant service isolation",
    "qualified practitioner lookup",
    "opening-hours validation",
    "holiday validation",
    "service duration validation",
    "practitioner overlap rejection",
    "customer overlap rejection",
    "booking cancellation releases a slot",
    "reschedule conflict rejection",
    "nearest available alternatives",
    "cross-organization booking isolation",
    "customer cannot use admin analytics",
]


def print_scenarios(title: str, scenarios: list[str]) -> None:
    print(f"\n{title}")
    for number, scenario in enumerate(scenarios, start=1):
        print(f"  {number:02d}. {scenario}")


def main() -> int:
    print_scenarios("Himali Tea House evaluations", STORE_SCENARIOS)
    print_scenarios("Lotus Wellness Studio evaluations", STUDIO_SCENARIOS)
    print("\nRunning deterministic database and authorization assertions…\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
