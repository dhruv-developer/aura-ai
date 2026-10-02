"""Durable chat conversations for the AURA app (multiple sessions per user).

Titles and message text are envelope-encrypted per row (AAD-bound to the account,
conversation and row), so a database dump alone never reveals what people asked.
``aura`` holds only content-free execution metadata (model, cost, routing, notes).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class _Sealed:
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class Conversation(_Sealed, Timestamps, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[str] = mapped_column(String(12), nullable=False, server_default="auto")

    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)


class ConversationMessage(_Sealed, Timestamps, Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(12), nullable=False)  # user | assistant
    aura: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # content-free numbers only
    # Optional sealed JSON with content about how the answer was made (e.g. the agent
    # tree: sub-questions, drafts, reviewer critiques). Encrypted like the message.
    trace_key_id: Mapped[str | None] = mapped_column(String(64))
    trace_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    trace_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    trace_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (Index("ix_conv_messages_conv_created", "conversation_id", "created_at"),)
