"""Tenant integrations: BYOK provider credentials (envelope-encrypted)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class ProviderCredential(Timestamps, Base):
    __tablename__ = "provider_credentials"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500))
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)  # KEK that wrapped the DEK
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One active credential per provider per account.
        Index(
            "uq_provider_credentials_active",
            "account_id",
            "provider",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
