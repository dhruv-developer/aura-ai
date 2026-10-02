"""Prompt registry: named templates with immutable versions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class PromptTemplate(Timestamps, Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (UniqueConstraint("account_id", "name", name="uq_prompt_name"),)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="CASCADE"), index=True, nullable=False
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # draft|active|archived
    origin: Mapped[str] = mapped_column(String(40), nullable=False, server_default="manual")
    evaluation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_prompt_version"),)


class PromptOptimization(Timestamps, Base):
    """An optimization run: eval inputs are sealed like shadow datasets and erased when
    the run finishes; results hold aggregates and the candidate version ids."""

    __tablename__ = "prompt_optimizations"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dataset_key_id: Mapped[str | None] = mapped_column(String(64))
    dataset_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    dataset_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    dataset_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    spent_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str | None] = mapped_column(String(64))
