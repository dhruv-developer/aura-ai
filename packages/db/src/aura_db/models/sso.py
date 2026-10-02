"""Enterprise identity: verified domains, SSO connections, SCIM tokens (spec §115)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class VerifiedDomain(Timestamps, Base):
    """A domain an organization proved it controls (DNS TXT). Only verified domains can
    be bound to SSO, and a domain belongs to at most one organization."""

    __tablename__ = "verified_domains"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SsoConnection(Timestamps, Base):
    __tablename__ = "sso_connections"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    protocol: Mapped[str] = mapped_column(String(8), nullable=False)  # oidc | saml
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="draft")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # no secrets
    default_role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="viewer")
    jit_provisioning: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # OIDC client secret, envelope-encrypted (AAD-bound to account and connection).
    key_id: Mapped[str | None] = mapped_column(String(64))
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)


class ScimToken(Timestamps, Base):
    __tablename__ = "scim_tokens"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
