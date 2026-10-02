"""Tenant context: the isolation boundary carried through every request."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from aura_shared.errors import Unauthenticated


class AccountKind(StrEnum):
    INDIVIDUAL = "individual"  # B2C personal account
    ORGANIZATION = "organization"  # B2B tenant


class PriorityClass(StrEnum):
    FREE = "free"
    PAID = "paid"
    B2B = "b2b"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True, slots=True)
class TenantContext:
    account_id: str
    kind: AccountKind
    workspace_id: str
    region: str
    priority: PriorityClass
    user_id: str | None = None
    organization_id: str | None = None
    environment_id: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.kind is AccountKind.ORGANIZATION and not self.organization_id:
            raise ValueError("organization tenants require organization_id")
        if self.kind is AccountKind.INDIVIDUAL and not self.user_id:
            raise ValueError("individual tenants require user_id")

    @property
    def tenant_key(self) -> str:
        """Stable key used for Redis hash tags and partitioning."""
        return self.account_id


_current: ContextVar[TenantContext | None] = ContextVar("aura_tenant", default=None)


def set_tenant(ctx: TenantContext) -> object:
    return _current.set(ctx)


def reset_tenant(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]


def current_tenant() -> TenantContext:
    ctx = _current.get()
    if ctx is None:
        raise Unauthenticated("no tenant bound to this request")
    return ctx
