"""Execution policies, immutable versions, and deployments (canary/rollback)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class ExecutionPolicyRow(Timestamps, Base):
    __tablename__ = "execution_policies"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # account | workload
    scope_ref: Mapped[str] = mapped_column(String(120), nullable=False, server_default="")
    active_version_id: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("account_id", "scope", "scope_ref", name="uq_policy_scope"),)


class PolicyVersion(Base):
    """Immutable once created. Status is lifecycle metadata, not content."""

    __tablename__ = "policy_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("execution_policies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    note: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (UniqueConstraint("policy_id", "version", name="uq_policy_version"),)


class Deployment(Timestamps, Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("execution_policies.id", ondelete="CASCADE"), nullable=False
    )
    from_version_id: Mapped[str | None] = mapped_column(String(40))
    to_version_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # canary|completed|rolled_back
    # canary: the monitor advances/rolls back automatically.
    # experiment: a fixed split (spec §152); only a human concludes it.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="canary", server_default="canary"
    )
    percent: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    rollback_reason: Mapped[str | None] = mapped_column(String(1000))
    history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        # At most one in-flight canary per policy.
        Index(
            "uq_deployments_inflight",
            "policy_id",
            unique=True,
            postgresql_where=text("status = 'canary'"),
        ),
    )
