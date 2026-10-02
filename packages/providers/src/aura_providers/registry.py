"""Provider registry and credential resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from aura_providers.domain.catalog import ModelSpec
from aura_providers.domain.types import Credentials, ProviderError, ProviderErrorKind
from aura_providers.ports import ModelProvider


class CredentialResolver(Protocol):
    async def resolve(self, account_id: str, provider: str) -> Credentials | None: ...


@dataclass(frozen=True, slots=True)
class PlatformCredentials:
    """AURA-owned provider keys (from the secrets manager / env). BYOK resolvers that
    read a tenant's own encrypted keys plug in ahead of this one."""

    keys: Mapping[str, Credentials]

    async def resolve(self, account_id: str, provider: str) -> Credentials | None:
        return self.keys.get(provider)


class ChainedCredentials:
    """First resolver that returns credentials wins (e.g. BYOK, then platform)."""

    def __init__(self, *resolvers: CredentialResolver) -> None:
        self._resolvers = resolvers

    async def resolve(self, account_id: str, provider: str) -> Credentials | None:
        for r in self._resolvers:
            if (creds := await r.resolve(account_id, provider)) is not None:
                return creds
        return None


class ProviderRegistry:
    def __init__(self, providers: Mapping[str, ModelProvider], credentials: CredentialResolver):
        self._providers = dict(providers)
        self._credentials = credentials

    def available(self) -> set[str]:
        return set(self._providers)

    def provider_for(self, spec: ModelSpec) -> ModelProvider:
        provider = self._providers.get(spec.provider)
        if provider is None:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, f"no adapter for {spec.provider}")
        return provider

    async def usable_providers(self, account_id: str) -> set[str]:
        """Providers this tenant can call right now (adapter + some credential)."""
        return {
            name
            for name in self._providers
            if await self._credentials.resolve(account_id, name) is not None
        }

    async def credential_sources(self, account_id: str) -> dict[str, str]:
        """Usable providers and where their credentials come from (platform / byok)."""
        out: dict[str, str] = {}
        for name in self._providers:
            creds = await self._credentials.resolve(account_id, name)
            if creds is not None:
                out[name] = creds.source
        return out

    async def credentials_for(self, account_id: str, spec: ModelSpec) -> Credentials:
        creds = await self._credentials.resolve(account_id, spec.provider)
        if creds is None:
            # Our configuration problem, not the model's: do not trip the circuit.
            raise ProviderError(ProviderErrorKind.AUTH, f"no credentials for {spec.provider}")
        return creds
