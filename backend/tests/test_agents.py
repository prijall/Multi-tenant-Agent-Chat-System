from dataclasses import dataclass

from sqlalchemy import select

from app.agents.contracts import AgentEvent, ProviderTurn, ToolCall
from app.agents.registry import registry_for
from app.agents.runtime import run_agent
from app.core.security import RequestContext
from app.db.models import Conversation, Organization, Product, ToolRun, User


@dataclass
class FakeProvider:
    turns: list[ProviderTurn]

    async def complete(
        self,
        *,
        system_prompt: str,
        events: list[AgentEvent],
        tools: list[dict],
    ) -> ProviderTurn:
        assert system_prompt
        assert events
        assert tools
        return self.turns.pop(0)


async def agent_context(session, slug: str) -> tuple[RequestContext, Conversation]:
    organization = Organization(slug=slug, name=slug)
    session.add(organization)
    await session.flush()
    user = User(org_id=organization.id, auth_subject=f"auth-{slug}", email=f"{slug}@test")
    session.add(user)
    await session.flush()
    conversation = Conversation(org_id=organization.id, user_id=user.id, title="Test")
    session.add(conversation)
    await session.commit()
    return RequestContext(user.id, organization.id, user.role), conversation


async def test_himali_registry_never_exposes_lotus_tools(session):
    context, _ = await agent_context(session, "himali-tea")

    tenant, registry = await registry_for(session, context)

    assert tenant == "himali-tea"
    assert "search_products" in registry.names
    assert "list_my_orders" in registry.names
    assert "find_available_slots" not in registry.names
    assert "list_my_bookings" not in registry.names


async def test_agent_rejects_cross_tenant_tool_call_and_audits_it(session):
    context, conversation = await agent_context(session, "himali-tea")
    provider = FakeProvider(
        [
            ProviderTurn(
                parts=[
                    {
                        "functionCall": {
                            "id": "wrong-tenant",
                            "name": "list_my_bookings",
                            "args": {},
                        }
                    }
                ],
                tool_calls=[ToolCall("wrong-tenant", "list_my_bookings", {})],
            ),
            ProviderTurn(
                parts=[{"text": "I cannot access booking tools from this organization."}],
                text="I cannot access booking tools from this organization.",
            ),
        ]
    )

    answer = await run_agent(session, context, conversation, "Show Lotus bookings", provider)
    audit = await session.scalar(select(ToolRun).where(ToolRun.conversation_id == conversation.id))

    assert answer.content == "I cannot access booking tools from this organization."
    assert audit.tool_name == "list_my_bookings"
    assert audit.status == "rejected"
    assert "not available" in audit.output_data["error"]


async def test_agent_uses_live_tenant_product_data(session):
    context, conversation = await agent_context(session, "himali-tea")
    session.add(
        Product(
            org_id=context.org_id,
            sku="TEA-1",
            name="First Flush",
            category="black tea",
            price=500,
            stock=8,
        )
    )
    await session.commit()
    provider = FakeProvider(
        [
            ProviderTurn(
                parts=[
                    {
                        "functionCall": {
                            "id": "products",
                            "name": "search_products",
                            "args": {"query": "First"},
                        }
                    }
                ],
                tool_calls=[ToolCall("products", "search_products", {"query": "First"})],
            ),
            ProviderTurn(
                parts=[{"text": "First Flush is in stock."}],
                text="First Flush is in stock.",
            ),
        ]
    )

    answer = await run_agent(session, context, conversation, "Do you have First Flush?", provider)
    audit = await session.scalar(select(ToolRun).where(ToolRun.conversation_id == conversation.id))

    assert answer.content == "First Flush is in stock."
    assert audit.output_data["products"][0]["sku"] == "TEA-1"
    assert audit.output_data["products"][0]["stock"] == 8


async def test_product_search_offers_real_in_stock_products_from_the_same_category(session):
    context, conversation = await agent_context(session, "himali-tea")
    session.add_all(
        [
            Product(
                org_id=context.org_id,
                sku="POT-1",
                name="Clay Pot",
                category="teaware",
                price=2200,
                stock=2,
            ),
            Product(
                org_id=context.org_id,
                sku="POT-2",
                name="Glass Pot",
                category="teaware",
                price=1800,
                stock=7,
            ),
            Product(
                org_id=context.org_id,
                sku="POT-3",
                name="Sold Out Pot",
                category="teaware",
                price=900,
                stock=0,
            ),
            Product(
                org_id=context.org_id,
                sku="TEA-1",
                name="Green Tea",
                category="tea",
                price=500,
                stock=9,
            ),
        ]
    )
    await session.commit()
    _, registry = await registry_for(session, context)

    output, _ = await registry.execute("search_products", {"query": "Clay"})

    assert [row["sku"] for row in output["products"]] == ["POT-1"]
    assert [row["sku"] for row in output["similar_in_stock"]] == ["POT-2"]
