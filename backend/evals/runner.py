"""make eval: run every scenario against the real model, and report pass or fail for each.

The fallback provider stays off, so a result always says which model earned it. FAIL means the
agent did the wrong thing; ERROR means the run couldn't finish, such as an outage or used-up quota.
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.agents.providers import build_primary_provider
from app.core.config import get_settings
from evals.harness import EvalDatabase, PacedProvider, Result, run_scenario
from evals.scenarios import ALL_SCENARIOS, HIMALI, LOTUS

RESULTS_DIR = Path(__file__).resolve().parent / "results"
# Gemini's free tier allows 15 requests a minute.
DEFAULT_SECONDS_PER_CALL = 4.2
# Several outages in a row mean the provider is down or throttling; carrying on would only wait.
MAX_ERRORS_IN_A_ROW = 3


def print_summary(results: list[Result], model: str, model_calls: int) -> None:
    print()
    for org, label in ((HIMALI, "Himali Tea House"), (LOTUS, "Lotus Wellness Studio")):
        scoped = [result for result in results if result.scenario.org == org]
        if not scoped:
            continue
        counts = Counter(result.status for result in scoped)
        print(
            f"{label}: {counts['PASS']}/{len(scoped)} passed, "
            f"{counts['FAIL']} failed, {counts['ERROR']} could not finish"
        )
    print(f"Model: {model} | Model calls: {model_calls} | Details: {results_file(model)}")


def results_file(model: str) -> Path:
    """One file per model, so trying another model never overwrites or mixes with these results."""
    return RESULTS_DIR / f"{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.json"


def finished_results(model: str) -> dict[tuple[str, str], dict]:
    """Scenarios a previous run did get an answer for, so a retry only spends quota on the rest."""
    path = results_file(model)
    if not path.exists():
        return {}
    earlier = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    return {(item["org"], item["name"]): item for item in earlier if item["status"] != "ERROR"}


def keep_earlier(
    selected: list, earlier: dict[tuple[str, str], dict], unfinished: bool
) -> dict[tuple[str, str], dict]:
    """Rerunning some scenarios replaces only those, so --only can't wipe the rest of a run."""
    if unfinished:
        return earlier
    rerun = {(scenario.org, scenario.name) for scenario in selected}
    return {key: item for key, item in earlier.items() if key not in rerun}


def save(results: list[Result], kept: dict[tuple[str, str], dict], model: str, schema: str) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    new = {(result.scenario.org, result.scenario.name): result.as_dict() for result in results}
    merged = {**kept, **new}
    ordered = [
        merged[(scenario.org, scenario.name)]
        for scenario in ALL_SCENARIOS
        if (scenario.org, scenario.name) in merged
    ]
    results_file(model).write_text(
        json.dumps(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "model": model,
                "schema": schema,
                "scenarios": ordered,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )


async def run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    provider = build_primary_provider(settings)
    if provider is None:
        print("Set LLM_PROVIDER and LLM_API_KEY first.", file=sys.stderr)
        return 2
    if settings.migration_database_url.startswith("sqlite"):
        print("Evals need Postgres: set DATABASE_URL_UNPOOLED.", file=sys.stderr)
        return 2
    scenarios = [
        scenario
        for scenario in ALL_SCENARIOS
        if (not arguments.only or arguments.only in scenario.name)
        and (not arguments.org or scenario.org == arguments.org)
    ]
    kept = keep_earlier(scenarios, finished_results(provider.model), arguments.unfinished)
    scenarios = [scenario for scenario in scenarios if (scenario.org, scenario.name) not in kept]
    paced = PacedProvider(provider, arguments.seconds_per_call)
    database = EvalDatabase(settings.migration_database_url)
    results: list[Result] = []
    print(f"Preparing a temporary schema ({database.schema}) and {len(scenarios)} scenarios…")
    await database.create()
    errors_in_a_row = 0
    try:
        for position, scenario in enumerate(scenarios):
            result = await run_scenario(database, scenario, paced)
            errors_in_a_row = errors_in_a_row + 1 if result.status == "ERROR" else 0
            results.append(result)
            # Saved as it goes, so a run that hits a quota still keeps what it learned.
            save(results, kept, provider.model, database.schema)
            print(
                f"{result.status:<5} {scenario.org}/{scenario.name}  "
                f"{result.seconds:.1f}s  {result.model_calls} calls"
                + (f"\n      {result.detail}" if result.detail else ""),
                flush=True,
            )
            if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                print(
                    f"Stopping with {len(scenarios) - position - 1} scenarios left: the provider "
                    "looks unavailable. Run again later with --unfinished."
                )
                break
    finally:
        await database.drop()
    print_summary(results, provider.model, paced.calls)
    return 0 if results and all(result.status == "PASS" for result in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Only scenarios whose name contains this")
    parser.add_argument("--org", choices=[HIMALI, LOTUS], help="Only this organisation")
    parser.add_argument(
        "--unfinished", action="store_true", help="Keep finished results; run only the rest"
    )
    parser.add_argument("--seconds-per-call", type=float, default=DEFAULT_SECONDS_PER_CALL)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
