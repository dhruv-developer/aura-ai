"""Roles and permissions. Pure: no I/O."""

from __future__ import annotations

from enum import StrEnum

from aura_shared.errors import Forbidden, ValidationFailed


class Permission(StrEnum):
    ACCOUNT_READ = "account:read"
    ACCOUNT_MANAGE = "account:manage"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    API_KEYS_READ = "api_keys:read"
    API_KEYS_MANAGE = "api_keys:manage"
    WORKLOADS_READ = "workloads:read"
    WORKLOADS_MANAGE = "workloads:manage"
    EXECUTIONS_READ = "executions:read"
    POLICIES_READ = "policies:read"
    POLICIES_MANAGE = "policies:manage"
    DEPLOYMENTS_MANAGE = "deployments:manage"
    EXECUTE = "runtime:execute"
    TELEMETRY_WRITE = "telemetry:write"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ENGINEER = "engineer"
    ANALYST = "analyst"
    BILLING = "billing"
    VIEWER = "viewer"


P = Permission
_VIEWER = frozenset(
    {P.ACCOUNT_READ, P.MEMBERS_READ, P.WORKLOADS_READ, P.EXECUTIONS_READ, P.POLICIES_READ}
)
_ANALYST = _VIEWER | {P.API_KEYS_READ}
_ENGINEER = _ANALYST | {
    P.WORKLOADS_MANAGE,
    P.POLICIES_MANAGE,
    P.API_KEYS_MANAGE,
    P.EXECUTE,
    P.TELEMETRY_WRITE,
}
_BILLING = _VIEWER | {P.BILLING_READ, P.BILLING_MANAGE}
_ALL = frozenset(Permission)
_ADMIN = _ALL - {P.ACCOUNT_MANAGE}  # owner-only: rename/delete org, transfer ownership

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.ANALYST: frozenset(_ANALYST),
    Role.ENGINEER: frozenset(_ENGINEER),
    Role.BILLING: frozenset(_BILLING),
    Role.ADMIN: _ADMIN,
    Role.OWNER: _ALL,
}

# The owner of a personal (B2C) account can do everything in it.
PERSONAL_ACCOUNT_PERMISSIONS: frozenset[Permission] = _ALL

# API keys are machine credentials: they may never manage members, billing or keys.
API_KEY_GRANTABLE: frozenset[Permission] = frozenset(
    {P.EXECUTE, P.TELEMETRY_WRITE, P.WORKLOADS_READ, P.EXECUTIONS_READ, P.POLICIES_READ}
)
DEFAULT_API_KEY_PERMISSIONS: frozenset[Permission] = frozenset({P.EXECUTE, P.TELEMETRY_WRITE})


def permissions_for(role: Role | str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[Role(role)]


def require(granted: frozenset[str] | frozenset[Permission], needed: Permission) -> None:
    if needed not in granted:
        raise Forbidden(f"missing permission {needed.value}")


def validate_api_key_permissions(
    requested: set[str], creator: frozenset[Permission]
) -> frozenset[Permission]:
    """A key may only hold grantable permissions the creator also holds (no escalation)."""
    try:
        perms = frozenset(Permission(p) for p in requested)
    except ValueError as exc:
        raise ValidationFailed(f"unknown permission: {exc}") from exc
    if not perms:
        raise ValidationFailed("an API key needs at least one permission")
    if not_grantable := perms - API_KEY_GRANTABLE:
        raise ValidationFailed(
            "not grantable to API keys: " + ", ".join(sorted(p.value for p in not_grantable))
        )
    if missing := perms - creator:
        raise Forbidden(
            "cannot grant permissions you do not hold: "
            + ", ".join(sorted(p.value for p in missing))
        )
    return perms


# Owner-only powers never go into a custom role.
CUSTOM_ROLE_GRANTABLE: frozenset[Permission] = _ALL - {P.ACCOUNT_MANAGE}


def validate_custom_role(
    requested: set[str], actor_permissions: frozenset[Permission]
) -> frozenset[Permission]:
    """A custom role is a non-empty subset of grantable permissions that the person
    creating it already holds (no privilege escalation through role design)."""
    if not requested:
        raise ValidationFailed("a role needs at least one permission")
    try:
        perms = frozenset(Permission(p) for p in requested)
    except ValueError as exc:
        raise ValidationFailed(f"unknown permission in {sorted(requested)}") from exc
    if not perms <= CUSTOM_ROLE_GRANTABLE:
        raise ValidationFailed("account:manage is owner-only and cannot be in a custom role")
    if not perms <= actor_permissions:
        raise Forbidden("you cannot grant permissions you do not hold")
    return perms


def check_assignable(
    granted: frozenset[Permission], actor_permissions: frozenset[Permission]
) -> None:
    if not granted <= actor_permissions:
        raise Forbidden("you cannot grant permissions you do not hold")
