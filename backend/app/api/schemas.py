import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.db.models import OrderStatus, UserRole


class OrderItemInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=100)


class OrderQuoteInput(BaseModel):
    items: list[OrderItemInput] = Field(min_length=1, max_length=50)


class OrderConfirmInput(OrderQuoteInput):
    delivery_address: str = Field(min_length=3, max_length=500)
    expected_total: Decimal = Field(ge=0)


class ReorderInput(BaseModel):
    expected_total: Decimal = Field(ge=0)


class BookingQuoteInput(BaseModel):
    service_id: uuid.UUID
    practitioner_id: uuid.UUID
    starts_at: datetime


class BookingConfirmInput(BookingQuoteInput):
    expected_price: Decimal = Field(ge=0)


class RescheduleInput(BaseModel):
    starts_at: datetime


class OrderStatusInput(BaseModel):
    status: OrderStatus


class MembershipInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: UserRole = UserRole.CUSTOMER


class MembershipRoleInput(BaseModel):
    role: UserRole


class ConversationInput(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class SlotChoiceInput(BaseModel):
    service_id: uuid.UUID
    practitioner_id: uuid.UUID
    starts_at: str = Field(min_length=10, max_length=40)
    booking_id: uuid.UUID | None = None


class ChatInput(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
