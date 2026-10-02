"""Request dependencies: container, principal resolution, account access, CSRF."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from aura_api.container import Container
from aura_auth.application.dto import AccountAccess, ApiKeyPrincipal, UserView
from aura_auth.domain.rbac import Permission
from aura_shared.errors import Forbidden, NotFound, Unauthenticated

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-aura-csrf"


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


@dataclass(frozen=True, slots=True)
class Principal:
    user: UserView | None = None
    api_key: ApiKeyPrincipal | None = None
    session_cookie: str | None = None


async def get_principal(request: Request, c: ContainerDep) -> Principal:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return Principal(api_key=await c.api_keys.authenticate(authz[7:]))
    cookie = request.cookies.get(c.settings.session_cookie_name)
    if cookie:
        session = await c.sessions.get(cookie)
        if session is not None:
            # Cookie auth is ambient, so unsafe methods need a header a cross-site form
            # cannot send (a CORS preflight would block it). This is the CSRF defence.
            if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
                raise Forbidden("missing CSRF header")
            try:
                user = await c.identity.get_user(session.user_id)
            except NotFound as exc:
                raise Unauthenticated("session no longer valid") from exc
            if user.status != "active":
                raise Forbidden("account suspended")
            return Principal(user=user, session_cookie=cookie)
    raise Unauthenticated("authentication required")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_user(principal: PrincipalDep) -> UserView:
    if principal.user is None:
        raise Forbidden("this endpoint requires a signed-in user")
    return principal.user


UserDep = Annotated[UserView, Depends(get_user)]


def account_access(
    permission: Permission,
) -> Callable[[str, Principal, Container], Awaitable[AccountAccess]]:
    """Dependency factory: resolve ``{account_id}`` and require ``permission`` on it."""

    async def dep(account_id: str, principal: PrincipalDep, c: ContainerDep) -> AccountAccess:
        if principal.user is not None:
            access = await c.identity.resolve_access(principal.user.id, account_id)
            if permission not in access.permissions:
                raise Forbidden(f"missing permission {permission.value}")
            return access
        key = principal.api_key
        assert key is not None
        if key.account_id != account_id:
            raise NotFound("account not found")
        if permission.value not in key.permissions:
            raise Forbidden(f"missing permission {permission.value}")
        # Machine principals only need identifiers downstream; they carry key permissions.
        return AccountAccess(
            account_id=account_id,
            kind="",
            tier="",
            status="active",
            region=c.settings.region,
            name="",
            role="api_key",
            permissions=frozenset(Permission(p) for p in key.permissions),
        )

    return dep
