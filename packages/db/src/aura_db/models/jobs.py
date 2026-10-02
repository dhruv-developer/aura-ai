"""Durable background jobs (Postgres queue with SKIP LOCKED) and shadow runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps, utcnow


class Job(Timestamps, Base):
    """Platform job queue. Payloads reference tenant rows by id and never hold tenant
    content (that stays in RLS-protected, encrypted tables)."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Client-side defaults too: tenants INSERT jobs but cannot SELECT them (no RETURNING).
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=text("now()"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(2000))

    __table_args__ = (
        Index(
            "ix_jobs_ready",
            "priority",
            "run_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class ShadowRun(Timestamps, Base):
    __tablename__ = "shadow_runs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # queued|running|completed|failed
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Replay inputs, envelope-encrypted; erased when the run finishes.
    dataset_key_id: Mapped[str | None] = mapped_column(String(64))
    dataset_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    dataset_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    dataset_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    spent_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str | None] = mapped_column(String(64))
