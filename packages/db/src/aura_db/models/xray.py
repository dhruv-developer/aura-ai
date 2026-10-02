"""X-Ray reports (aggregates only: prompt text is never stored) and sales leads."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class XRayReport(Timestamps, Base):
    __tablename__ = "xray_reports"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # upload:jsonl | gateway ...
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ready | failed
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str | None] = mapped_column(String(64))


class Lead(Timestamps, Base):
    """Public X-Ray funnel submissions (spec §25). Platform-level, not tenant data."""

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    company: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, server_default="new")
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
