"""OAuth/OIDC login flow (authorization code + PKCE) with Redis-held state."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

import redis.asyncio as aioredis

from aura_auth.application.dto import UserView
from aura_auth.application.identity import IdentityService
from aura_auth.application.ports import OAuthProvider
from aura_shared.errors import Unauthenticated
from aura_shared.redis import KeyFamily, build_key, ttl_for


class OAuthLoginService:
    def __init__(
        self,
        *,
        state: aioredis.Redis,
        identity: IdentityService,
        providers: dict[str, OAuthProvider],
        redirect_base: str,
    ) -> None:
        self._r = state
        self._identity = identity
        self._providers = providers
        self._redirect_base = redirect_base.rstrip("/")

    def _provider(self, name: str) -> OAuthProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise Unauthenticated(f"login with {name} is not configured")
        return provider

    def _redirect_uri(self, name: str) -> str:
        return f"{self._redirect_base}/auth/{name}/callback"

    async def start(self, name: str, *, return_to: str = "/") -> str:
        provider = self._provider(name)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        # Only same-site relative paths are accepted, which prevents open redirects.
        safe_return = (
            return_to
            if return_to.startswith("/") and "//" not in return_to and "\\" not in return_to
            else "/"
        )
        await self._r.set(
            build_key(KeyFamily.OAUTH_STATE, "_global", state),
            json.dumps({"v": verifier, "p": name, "r": safe_return}),
            ex=ttl_for(KeyFamily.OAUTH_STATE, jitter=False),
        )
        return provider.authorization_url(
            state=state, code_challenge=challenge, redirect_uri=self._redirect_uri(name)
        )

    async def finish(self, name: str, *, code: str, state: str) -> tuple[UserView, str]:
        raw = await self._r.getdel(build_key(KeyFamily.OAUTH_STATE, "_global", state))
        if raw is None:
            raise Unauthenticated("login session expired; try again")
        data = json.loads(raw)
        if data["p"] != name:
            raise Unauthenticated("provider mismatch")
        info = await self._provider(name).exchange(
            code=code, code_verifier=data["v"], redirect_uri=self._redirect_uri(name)
        )
        user = await self._identity.login_with_oauth(
            provider=name,
            subject=info.subject,
            email=info.email,
            email_verified=info.email_verified,
            name=info.name,
        )
        return user, str(data["r"])
