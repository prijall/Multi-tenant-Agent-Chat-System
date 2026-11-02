import asyncio
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import RequestContext
from app.db.database import create_engine
from app.db.models import Organization, Product, User, UserRole
from app.db.repositories import list_products


async def smoke_test() -> None:
    """Exercise the pooled Neon connection and tenant-scoped repository end to end."""
    engine = create_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    marker = uuid.uuid4().hex[:12]
    try:
        async with factory() as session:
            async with session.begin():
                organization = Organization(slug=f"smoke-{marker}", name="Smoke Test Tenant")
                session.add(organization)
                await session.flush()
                user = User(
                    org_id=organization.id,
                    auth_subject=f"smoke-auth-{marker}",
                    email=f"smoke-{marker}@example.invalid",
                    role=UserRole.CUSTOMER,
                )
                product = Product(
                    org_id=organization.id,
                    sku=f"SMOKE-{marker}",
                    name="Neon connection test",
                    category="test",
                    price=Decimal("1.00"),
                    stock=1,
                )
                session.add_all([user, product])
                await session.flush()

                context = RequestContext(user.id, organization.id, user.role)
                products = await list_products(session, context)
                if [row.sku for row in products] != [product.sku]:
                    raise RuntimeError("Tenant-scoped Neon query returned unexpected rows")

                await session.rollback()
        print("Neon pooled connection and tenant-scoped query passed.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(smoke_test())

