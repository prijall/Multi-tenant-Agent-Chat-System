import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import accounts, admin, bookings, conversations, health, knowledge, orders
from app.core.config import get_settings
from app.services.knowledge import embed_query


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # In the background, so the first customer doesn't wait for the model to load.
    warm_up = asyncio.create_task(asyncio.to_thread(embed_query, "warm up"))
    yield
    warm_up.cancel()


app = FastAPI(title="Multi-Tenant Agent Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (health, accounts, conversations, knowledge, orders, bookings, admin):
    app.include_router(module.router)
