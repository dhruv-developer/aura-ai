"""Identity and tenancy: users, accounts (B2C individual | B2B organization), orgs,
memberships, workspaces, projects, environments, API keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aura_db.base import ID, Base, Timestamps


class User(Timestamps, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    is_superadmin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True)

    __table_args__ = (
        Index("uq_users_email_lower", func.lower(text("email")), unique=True),
        CheckConstraint("status in ('active','suspended')", name="status"),
    )


class OAuthIdentity(Timestamps, Base):
    __tablename__ = "oauth_identities"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))

    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),)


class Account(Timestamps, Base):
    """The billing/tenancy root. Individual = B2C personal; organization = B2B tenant."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    region: Mapped[str] = mapped_column(String(16), nullable=False, server_default="in")
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    learning_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Autopilot: proven shadow results are deployed as canaries automatically (V4).
    autopilot: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        CheckConstraint("kind in ('individual','organization')", name="kind"),
        CheckConstraint("tier in ('b2c','b2b','enterprise')", name="tier"),
        CheckConstraint("status in ('active','suspended')", name="status"),
        CheckConstraint(
            "kind <> 'individual' OR owner_user_id IS NOT NULL", name="individual_has_owner"
        ),
        # One personal account per user.
        Index(
            "uq_accounts_personal_owner",
            "owner_user_id",
            unique=True,
            postgresql_where=text("kind = 'individual'"),
        ),
    )


class Organization(Timestamps, Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)


class Membership(Timestamps, Base):
    __tablename__ = "memberships"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # A custom role (advanced RBAC) overrides ``role``'s permissions; ``role`` stays the
    # fallback if the custom role is deleted.
    custom_role_id: Mapped[str | None] = mapped_column(
        ForeignKey("custom_roles.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),
        CheckConstraint(
            "role in ('owner','admin','engineer','analyst','billing','viewer')", name="role"
        ),
    )


class CustomRole(Timestamps, Base):
    __tablename__ = "custom_roles"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)

    __table_args__ = (UniqueConstraint("account_id", "name", name="uq_custom_role_name"),)


class Workspace(Timestamps, Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        Index(
            "uq_workspaces_default", "account_id", unique=True, postgresql_where=text("is_default")
        ),
    )


class Project(Timestamps, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)

    __table_args__ = (UniqueConstraint("workspace_id", "slug", name="uq_project_ws_slug"),)


class Environment(Timestamps, Base):
    __tablename__ = "environments"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "kind", name="uq_env_project_kind"),
        CheckConstraint("kind in ('development','staging','production')", name="kind"),
    )


class ApiKey(Timestamps, Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    environment_id: Mapped[str | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
