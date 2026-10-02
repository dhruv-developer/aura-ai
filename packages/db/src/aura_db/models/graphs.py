"""Asynchronous execution-graph runs. Goal and result are envelope-encrypted."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, LargeBinary, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class GraphRun(Timestamps, Base):
    __tablename__ = "graph_runs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    template: Mapped[str | None] = mapped_column(String(32))
    request: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # non-content options (scopes, budget, policy refs)
    trace: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    key_id: Mapped[str | None] = mapped_column(String(64))
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)  # sealed {"goal", "output"}
    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(String(2000))
