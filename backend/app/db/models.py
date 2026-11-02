import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class OrderStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class PendingActionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


EMBEDDING_DIMENSIONS = 384


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kathmandu")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    auth_subject: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.CUSTOMER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    organization: Mapped[Organization] = relationship()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("org_id", "sku", name="uq_product_org_sku"),
        CheckConstraint("stock >= 0", name="ck_product_stock_nonnegative"),
        CheckConstraint("price >= 0", name="ck_product_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(100), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    stock: Mapped[int]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total >= 0", name="ck_order_total_nonnegative"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    payment_method: Mapped[str] = mapped_column(String(40), default="cash_on_delivery")
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    delivery_address: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["OrderItem"]] = relationship(cascade="all, delete-orphan")
    history: Mapped[list["OrderStatusEvent"]] = relationship(
        cascade="all, delete-orphan", order_by="OrderStatusEvent.at"
    )


class OrderStatusEvent(Base):
    __tablename__ = "order_status_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_item_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))



class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_service_org_code"),
        CheckConstraint("duration_minutes > 0", name="ck_service_duration_positive"),
        CheckConstraint("price >= 0", name="ck_service_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(100))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class Practitioner(Base):
    __tablename__ = "practitioners"
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_practitioner_org_code"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))

    services: Mapped[list[Service]] = relationship(secondary="practitioner_services")


class PractitionerService(Base):
    __tablename__ = "practitioner_services"

    practitioner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("practitioners.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )


class OpeningHours(Base):
    """Weekly hours in the organisation's local time; a weekday with no row is closed."""

    __tablename__ = "opening_hours"
    __table_args__ = (
        UniqueConstraint("org_id", "weekday", name="uq_opening_hours_org_weekday"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_opening_hours_weekday"),
        CheckConstraint("opens_at < closes_at", name="ck_opening_hours_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    opens_at: Mapped[time]
    closes_at: Mapped[time]


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("org_id", "day", name="uq_holiday_org_day"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    day: Mapped[date]
    name: Mapped[str] = mapped_column(String(160))


class Booking(Base):
    """Overlap safety lives in Postgres exclusion constraints (see bootstrap_database)."""

    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_booking_time_order"),
        CheckConstraint("price >= 0", name="ck_booking_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    practitioner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practitioners.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.CONFIRMED
    )
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    late_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    """One retrievable passage; every search filters on org_id inside the SQL query."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    document: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(String(300))
    section: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        cascade="all, delete-orphan", order_by="ChatMessage.created_at"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    cards: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    # Set here because Postgres now() is fixed per transaction, which would tie a turn's messages.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), index=True)
    input_data: Mapped[dict[str, object]] = mapped_column(JSON)
    output_data: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30))
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingAction(Base):
    """A change a write tool proposed; only the customer's confirmation carries it out."""

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[PendingActionStatus] = mapped_column(
        Enum(PendingActionStatus), default=PendingActionStatus.PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
