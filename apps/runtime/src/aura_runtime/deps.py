"""Runtime request authentication → ExecutionContext."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Request
from redis.exceptions import RedisError

from aura_auth.domain.rbac import Permission
from aura_engine import ExecutionContext
from aura_runtime.container import RuntimeContainer
from aura_shared.errors import Forbidden, RateLimited, Unauthenticated
from aura_shared.redis import KeyFamily, build_key

log = logging.getLogger("aura.runtime")


def get_container(request: Request) -> RuntimeContainer:
    c: RuntimeContainer = request.app.state.container
    return c


ContainerDep = Annotated[RuntimeContainer, Depends(get_container)]


CSRF_HEADER = "x-aura-csrf"
ACCOUNT_HEADER = "x-aura-account"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _rate_limit(c: RuntimeContainer, account_id: str, subject: str) -> None:
    try:
        rl = await c.scripts.rate_limit(
            build_key(KeyFamily.RATE_LIMIT, account_id, "runtime", subject),
            rate_per_s=c.settings.runtime_rate_per_key_per_s,
            burst=c.settings.runtime_burst_per_key,
        )
    except (RedisError, OSError, TimeoutError):
        # Rate limiting protects capacity; it fails open rather than causing an outage.
        log.error("rate limiter unavailable; failing open", exc_info=True)
        return
    if not rl.allowed:
        raise RateLimited("rate limit exceeded", retry_after_ms=rl.retry_after_ms)


async def _session_context(request: Request, c: RuntimeContainer, cookie: str) -> ExecutionContext:
    """The web app's own session. No ``api_access`` entitlement is needed: this is the
    product UI, not the API. Same CSRF rule as the control plane."""
    session = await c.sessions.get(cookie)
    if session is None:
        raise Unauthenticated("session expired; sign in again")
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise Forbidden("missing CSRF header")
    access = await c.session_access.resolve(
        session.user_id, request.headers.get(ACCOUNT_HEADER) or None
    )
    await _rate_limit(c, access.account_id, f"user_{session.user_id}")
    ents = await c.entitlements.get(access.account_id)
    plan = c.billing_catalog.plans.get(ents.plan_code or "")
    return ExecutionContext(
        account_id=access.account_id,
        workspace_id=access.workspace_id,
        region=access.region,
        priority=plan.priority_class if plan else "free",
        plan_code=ents.plan_code,
        user_id=session.user_id,
    )


async def execution_context(request: Request, c: ContainerDep) -> ExecutionContext:
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        cookie = request.cookies.get(c.settings.session_cookie_name)
        if cookie:
            return await _session_context(request, c, cookie)
        raise Unauthenticated("send an AURA API key as 'Authorization: Bearer aura_sk_...'")
    key = await c.api_keys.authenticate(authz[7:])
    if Permission.EXECUTE.value not in key.permissions:
        raise Forbidden("this API key lacks runtime:execute")
    await _rate_limit(c, key.account_id, key.api_key_id)
    ents = await c.entitlements.require(key.account_id, "api_access")
    plan = c.billing_catalog.plans.get(ents.plan_code or "")
    return ExecutionContext(
        account_id=key.account_id,
        workspace_id=key.workspace_id,
        region=key.region,
        priority=plan.priority_class if plan else "free",
        plan_code=ents.plan_code,
        environment_id=key.environment_id,
        api_key_id=key.api_key_id,
    )


ContextDep = Annotated[ExecutionContext, Depends(execution_context)]
