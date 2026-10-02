"""Data returned by auth application services (decoupled from ORM rows)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aura_auth.domain.rbac import Permission


@dataclass(frozen=True, slots=True)
class UserView:
    id: str
    email: str
    name: str | None
    status: str
    is_superadmin: bool


@dataclass(frozen=True, slots=True)
class AccountAccess:
    """What a user can do in one account (their personal account or an org)."""

    account_id: str
    kind: str  # individual | organization
    tier: str
    status: str
    region: str
    name: str
    role: str  # "owner" for the personal account
    permissions: frozenset[Permission]
    organization_id: str | None = None
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class OrganizationView:
    id: str
    account_id: str
    name: str
    slug: str
    workspace_id: str
    project_id: str
    environment_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class ApiKeyView:
    id: str
    account_id: str
    environment_id: str | None
    name: str
    prefix: str
    permissions: list[str]
    created_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    api_key_id: str
    account_id: str
    environment_id: str | None
    permissions: frozenset[str]
    workspace_id: str = ""
    region: str = "in"
