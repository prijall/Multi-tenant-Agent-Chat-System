from dataclasses import dataclass, field

import pytest

from evals.checks import orders_are, reply_avoids, reply_mentions
from evals.harness import RESEEDED_TABLES, CheckFailed, PacedProvider
from evals.scenarios import ALL_SCENARIOS, HIMALI, LOTUS


@pytest.mark.parametrize("org", [HIMALI, LOTUS])
def test_each_agent_has_the_scenarios_the_brief_requires(org):
    scenarios = [scenario for scenario in ALL_SCENARIOS if scenario.org == org]
    covered = " ".join(scenario.covers for scenario in scenarios)

    assert len(scenarios) >= 12
    assert len({scenario.name for scenario in scenarios}) == len(scenarios)
    for required in (
        "knowledge answer",
        "unanswerable",
        "happy path",
        "memory",
        "cross-organization",
        "cross-customer",
        "prompt injection",
    ):
        assert required in covered


def test_resets_never_touch_the_expensive_embeddings_or_organisations():
    assert "knowledge_chunks" not in RESEEDED_TABLES
    assert "organizations" not in RESEEDED_TABLES
    assert "orders" in RESEEDED_TABLES


@dataclass
class FakeReply:
    content: str


@dataclass
class FakeWorld:
    reply: str = ""
    count: int = 0
    values: dict = field(default_factory=dict)

    @property
    def last_reply(self):
        return FakeReply(self.reply)

    async def order_count(self):
        return self.count


async def test_checks_fail_with_a_reason():
    with pytest.raises(CheckFailed, match="expected 1"):
        await orders_are(1)(FakeWorld(count=0))
    with pytest.raises(CheckFailed, match="leaks"):
        await reply_avoids("Secret")(FakeWorld(reply="the secret is out"))
    with pytest.raises(CheckFailed, match="mentions none"):
        await reply_mentions("777")(FakeWorld(reply="It costs Rs 750"))


async def test_checks_pass_on_loose_rewording():
    await reply_mentions("Shipped")(FakeWorld(reply="It has already shipped."))
    await orders_are(0)(FakeWorld(count=0))


async def test_pacing_keeps_requests_apart_and_counts_them():
    class Provider:
        async def complete(self, **_):
            return "done"

    paced = PacedProvider(Provider(), seconds_per_call=0.02)

    assert await paced.complete() == "done"
    assert await paced.complete() == "done"
    assert paced.calls == 2
