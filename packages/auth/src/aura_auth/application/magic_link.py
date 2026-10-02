"""Passwordless email login: single-use, short-lived, rate-limited links."""

from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlencode

import redis.asyncio as aioredis

from aura_auth.application.dto import UserView
from aura_auth.application.identity import IdentityService
from aura_auth.application.ports import EmailMessage, Mailer
from aura_auth.domain.emails import normalize_email
from aura_shared.errors import RateLimited, Unauthenticated, ValidationFailed
from aura_shared.redis import KeyFamily, RedisScripts, build_key, ttl_for

GLOBAL = "_global"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


class MagicLinkService:
    def __init__(
        self,
        *,
        state: aioredis.Redis,
        scripts: RedisScripts,
        identity: IdentityService,
        mailer: Mailer,
        verify_url: str,
        app_verify_urls: dict[str, str] | None = None,
        per_email_per_hour: int = 5,
        per_ip_per_hour: int = 30,
    ) -> None:
        self._r = state
        self._scripts = scripts
        self._identity = identity
        self._mailer = mailer
        self._verify_urls = {"web": verify_url, **(app_verify_urls or {})}
        self._per_email = per_email_per_hour
        self._per_ip = per_ip_per_hour

    async def request(self, raw_email: str, *, ip: str | None, app: str = "web") -> None:
        """``app`` selects the verify page from a server-side allowlist (never a URL
        from the client, which would make this an open redirect / token leak)."""
        if app not in self._verify_urls:
            raise ValidationFailed("unknown app")
        email = normalize_email(raw_email)
        # Keys use hashes so no PII ends up in Redis key names.
        checks = [(f"email:{_h(email)}", self._per_email)]
        if ip:
            checks.append((f"ip:{_h(ip)}", self._per_ip))
        for part, per_hour in checks:
            kind, digest = part.split(":")
            res = await self._scripts.rate_limit(
                build_key(KeyFamily.RATE_LIMIT, GLOBAL, "magic", kind, digest),
                rate_per_s=per_hour / 3600,
                burst=per_hour,
            )
            if not res.allowed:
                raise RateLimited(
                    "too many login links requested", retry_after_ms=res.retry_after_ms
                )

        link = await self.issue_link(email, app=app)
        await self._mailer.send(
            EmailMessage(
                to=email,
                subject="Your AURA sign-in link",
                text=f"Sign in to AURA:\n\n{link}\n\nThis link expires in 15 minutes and "
                "can be used once. If you did not request it, ignore this email.",
            )
        )

    async def issue_link(self, raw_email: str, *, app: str = "web") -> str:
        """Mint a one-time sign-in link without sending it. ``request`` mails it; local
        tooling (``aura-api login-link``, dev only) prints it."""
        if app not in self._verify_urls:
            raise ValidationFailed("unknown app")
        token = secrets.token_urlsafe(32)
        await self._r.set(
            build_key(KeyFamily.MAGIC_LINK, GLOBAL, _h(token)),
            normalize_email(raw_email),
            ex=ttl_for(KeyFamily.MAGIC_LINK, jitter=False),
        )
        return f"{self._verify_urls[app]}?{urlencode({'token': token})}"

    async def verify(self, token: str) -> UserView:
        if not token or len(token) > 200:
            raise Unauthenticated("invalid or expired link")
        raw = await self._r.getdel(build_key(KeyFamily.MAGIC_LINK, GLOBAL, _h(token)))
        if raw is None:
            raise Unauthenticated("invalid or expired link")
        email = raw.decode() if isinstance(raw, bytes) else str(raw)
        return await self._identity.login_with_email(email)
