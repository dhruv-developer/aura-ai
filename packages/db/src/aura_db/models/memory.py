"""Long-term memory and knowledge-base chunks (ADR-0004, ADR-0009)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    REAL,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps, utcnow


class MemoryItem(Timestamps, Base):
    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # scope = user | workspace | workload | kb (knowledge base); scope_ref names it.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # semantic|episodic|preference|document
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        # english (stemmed) for morphology + simple (language-agnostic) for everything else
        Computed(
            "to_tsvector('english', content) || to_tsvector('simple', content)", persisted=True
        ),
    )
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    importance: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5, server_default="0.5"
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.8, server_default="0.8"
    )
    source: Mapped[str] = mapped_column(
        String(120), nullable=False, default="api", server_default="api"
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_memory_items_tsv", "tsv", postgresql_using="gin"),
        Index("ix_memory_items_scope", "account_id", "scope", "scope_ref"),
    )
