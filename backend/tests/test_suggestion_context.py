from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.agents.contracts import ProviderTurn, ToolCall
from app.agents.runtime import run_agent
from app.agents.suggestions import suggest_next_message
from app.core.security import RequestContext
from app.db.models import (
    ChatMessage,
    Conversation,
    MessageRole,
    Order,
    OrderItem,
    OrderStatus,
    Organization,
    Practitioner,
    Product,
    Service,
    ToolRun,
    User,
)

ANSWER = ProviderTurn(parts=[{"text": "Here you go"}], text="Here you go")


def tool_turn(name: str, arguments: dict | None = None) -> ProviderTurn:
    args = arguments or {}
    return ProviderTurn(
        parts=[{"functionCall": {"name": name, "args": args}}],
        tool_calls=[ToolCall(name, name, args)],
    )


@dataclass
class Scripted:
    turns: list[ProviderTurn]

    async def complete(self, *, system_prompt, events, tools) -> ProviderTurn:
        return self.turns.pop(0)


@dataclass
class Recording:
    calls: list[dict] = field(default_factory=list)

    async def complete(self, *, system_prompt, events, tools) -> ProviderTurn:
        self.calls.append({"system": system_prompt, "text": events[0].parts[0]["text"]})
        return ProviderTurn(parts=[{"text": "Next please"}], text="Next please")


async def customer_chat(session, slug: str):
    org = Organization(slug=slug, name="The Org")
    session.add(org)
    await session.flush()
    user = User(org_id=org.id, email=f"{slug}@test")
    session.add(user)
    await session.flush()
    conversation = Conversation(org_id=org.id, user_id=user.id, title="Chat")
    session.add(conversation)
    await session.commit()
    return org, user, conversation, RequestContext(user.id, org.id, user.role)


async def test_it_sees_products_including_what_is_out_of_stock(session):
    org, _, conversation, context = await customer_chat(session, "himali-tea")
    session.add_all(
        [
            Product(org_id=org.id, sku="T1", name="Jasmine Green", category="green",
                    price=Decimal("750"), stock=20),
            Product(org_id=org.id, sku="T2", name="Smoked Green", category="green",
                    price=Decimal("850"), stock=0),
        ]
    )
    await session.commit()
    await run_agent(
        session, context, conversation, "Green teas?",
        Scripted([tool_turn("search_products", {"query": "Green"}), ANSWER]),
    )
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    text = provider.calls[0]["text"]
    assert "Jasmine Green, Rs 750.00, 20 in stock" in text
    assert "Smoked Green, Rs 850.00, out of stock" in text


async def test_delivery_addresses_and_ids_are_not_sent(session):
    org, user, conversation, context = await customer_chat(session, "himali-tea")
    product = Product(org_id=org.id, sku="T1", name="Jasmine Green", category="green",
                      price=Decimal("750"), stock=20)
    session.add(product)
    await session.flush()
    order = Order(org_id=org.id, customer_id=user.id, status=OrderStatus.SHIPPED,
                  total=Decimal("750"), delivery_address="42 Secret Lane")
    session.add(order)
    await session.flush()
    session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1,
                          unit_price=Decimal("750")))
    await session.commit()
    order_id = str(order.id)
    await run_agent(
        session, context, conversation, "Where is my order?",
        Scripted([tool_turn("get_order_status", {"order_id": order_id}), ANSWER]),
    )
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    text = provider.calls[0]["text"]
    assert "shipped order of Jasmine Green" in text
    assert "Secret Lane" not in text
    assert order_id not in text


async def test_only_the_latest_questions_lookups_count(session):
    org, _, conversation, context = await customer_chat(session, "himali-tea")
    session.add(Product(org_id=org.id, sku="T1", name="Old Lookup Tea", category="green",
                        price=Decimal("1"), stock=1))
    await session.commit()
    await run_agent(
        session, context, conversation, "Tea?",
        Scripted([tool_turn("search_products", {"query": "Old"}), ANSWER]),
    )
    await run_agent(session, context, conversation, "Thanks", Scripted([ANSWER]))
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    assert "Live facts" not in provider.calls[0]["text"]


async def test_failed_lookups_are_not_presented_as_facts(session):
    _, _, conversation, context = await customer_chat(session, "himali-tea")
    await run_agent(
        session, context, conversation, "Track it",
        Scripted([tool_turn("get_order_status", {"order_id": "not-a-uuid"}), ANSWER]),
    )
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    assert "Live facts" not in provider.calls[0]["text"]


@pytest.mark.parametrize(
    ("slug", "can", "cannot"),
    [
        ("himali-tea", "place an order", "book an appointment"),
        ("lotus-wellness", "book an appointment", "place an order"),
    ],
)
async def test_it_is_told_only_what_this_organizations_agent_can_do(session, slug, can, cannot):
    _, _, conversation, _ = await customer_chat(session, slug)
    session.add(
        ChatMessage(conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="Hi",
                    citations=[])
    )
    await session.commit()
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    assert can in provider.calls[0]["system"]
    assert cannot not in provider.calls[0]["system"]


async def test_another_customers_lookups_never_appear(session):
    org, _, conversation, context = await customer_chat(session, "himali-tea")
    other = User(org_id=org.id, email="other@test")
    session.add(other)
    await session.flush()
    theirs = Conversation(org_id=org.id, user_id=other.id, title="Theirs")
    session.add(theirs)
    await session.flush()
    question = ChatMessage(conversation_id=theirs.id, role=MessageRole.USER, content="Mine",
                           citations=[])
    session.add(question)
    await session.flush()
    session.add(
        ToolRun(conversation_id=theirs.id, message_id=question.id, tool_name="search_products",
                input_data={}, status="completed", duration_ms=1,
                output_data={"products": [{"name": "Their Secret", "price": "1", "stock": 1}]})
    )
    await session.commit()
    await run_agent(session, context, conversation, "Hello", Scripted([ANSWER]))
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    assert "Their Secret" not in provider.calls[0]["text"]


async def test_lotus_slots_and_services_become_facts(session):
    org, _, conversation, context = await customer_chat(session, "lotus-wellness")
    session.add_all(
        [
            Service(org_id=org.id, code="m", name="Swedish", category="massage",
                    duration_minutes=60, price=Decimal("3500")),
            Practitioner(org_id=org.id, code="p", name="Sita"),
        ]
    )
    await session.commit()
    await run_agent(
        session, context, conversation, "Massages?",
        Scripted([tool_turn("list_services"), ANSWER]),
    )
    provider = Recording()

    await suggest_next_message(session, conversation, provider)

    assert "Services: Swedish, Rs 3500.00" in provider.calls[0]["text"]
