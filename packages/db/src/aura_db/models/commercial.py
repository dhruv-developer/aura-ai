"""Plans, versioned entitlements, subscriptions, credit ledger, usage, invoices."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class Feature(Timestamps, Base):
    __tablename__ = "features"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # boolean | metered | gauge
    unit: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")

    __table_args__ = (CheckConstraint("kind in ('boolean','metered','gauge')", name="kind"),)


class Plan(Timestamps, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    audience: Mapped[str] = mapped_column(String(8), nullable=False)  # b2c | b2b
    priority_class: Mapped[str] = mapped_column(String(16), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("plan_versions.id", use_alter=True, name="fk_plans_current_version")
    )

    __table_args__ = (CheckConstraint("audience in ('b2c','b2b')", name="audience"),)


class PlanVersion(Base):
    """Immutable snapshot of a plan's price + entitlements. Subscriptions pin a version,
    so pricing/packaging changes never silently alter existing customers (grandfathering)."""

    __tablename__ = "plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int | None] = mapped_column(BigInteger)  # None = custom contract
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    interval: Mapped[str] = mapped_column(String(8), nullable=False, server_default="month")
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (UniqueConstraint("plan_code", "version", name="uq_plan_version"),)


class PlanEntitlement(Base):
    __tablename__ = "plan_entitlements"

    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), primary_key=True
    )
    feature_key: Mapped[str] = mapped_column(ForeignKey("features.key"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    limit: Mapped[int | None] = mapped_column(BigInteger)  # None = unlimited
    period: Mapped[str | None] = mapped_column(String(8))  # day | month | None


class EntitlementOverride(Timestamps, Base):
    __tablename__ = "entitlement_overrides"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    feature_key: Mapped[str] = mapped_column(ForeignKey("features.key"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    limit: Mapped[int | None] = mapped_column(BigInteger)
    period: Mapped[str | None] = mapped_column(String(8))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


LIVE_SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "paused")


class Subscription(Timestamps, Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_versions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    __table_args__ = (
        CheckConstraint(
            "status in ('incomplete','trialing','active','past_due','paused','canceled')",
            name="status",
        ),
        # At most one live subscription per account.
        Index(
            "uq_subscriptions_live",
            "account_id",
            unique=True,
            postgresql_where=text("status in ('trialing','active','past_due','paused')"),
        ),
    )


class CreditWallet(Timestamps, Base):
    """Materialised balance for fast reads; the ledger is the source of truth."""

    __tablename__ = "credit_wallets"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(128))  # idempotency key
    memo: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("account_id", "reference", name="uq_ledger_txn_reference"),
        CheckConstraint(
            "kind in ('grant','purchase','consume','refund','expire','adjust')", name="kind"
        ),
    )


class LedgerEntry(Base):
    """Double-entry postings. Entries of one transaction must sum to zero (DB trigger)."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    book: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "book in ('customer','system_grants','system_sales','system_usage',"
            "'system_expiry','system_adjust')",
            name="book",
        ),
        CheckConstraint("amount <> 0", name="nonzero"),
    )


class UsageRecord(Base):
    """Durable snapshot of metered usage per account/metric/period (reconciled from Redis)."""

    __tablename__ = "usage_records"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)  # 2026-10 | 2026-10-01
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Invoice(Timestamps, Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey("subscriptions.id"))
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_invoice_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_url: Mapped[str | None] = mapped_column(String(500))


class PaymentEvent(Base):
    """Every webhook, stored once (idempotency) before processing."""

    __tablename__ = "payment_events"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_event_provider_id"),
    )
