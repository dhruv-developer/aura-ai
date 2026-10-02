"""Shared provider-stack wiring (runtime and workers must build it identically)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from aura_billing.application.entitlements import EntitlementService
from aura_cache import TieredCache
from aura_credentials import ProviderCredentialService
from aura_db import Database
from aura_providers.adapters.anthropic_provider import AnthropicProvider
from aura_providers.adapters.openai_compat import OpenAICompatibleProvider
from aura_providers.adapters.stub import StubProvider
from aura_providers.domain.catalog import ModelCatalog
from aura_providers.domain.catalog import load_catalog as load_model_catalog
from aura_providers.domain.types import Credentials
from aura_providers.ports import ModelProvider
from aura_providers.registry import (
    ChainedCredentials,
    CredentialResolver,
    PlatformCredentials,
    ProviderRegistry,
)
from aura_shared.crypto import Envelope, envelope_from_settings
from aura_shared.settings import Environment, Settings

# Stub credentials never leave the process; they only satisfy the registry contract.
STUB_CREDENTIALS = Credentials(api_key="stub", source="platform")


class EntitledByok:
    """Use a tenant's own key only while their plan includes BYOK. A downgrade stops
    BYOK use immediately (entitlements cache invalidation) without deleting the key."""

    def __init__(self, byok: ProviderCredentialService, entitlements: EntitlementService):
        self._byok = byok
        self._ent = entitlements

    async def resolve(self, account_id: str, provider: str) -> Credentials | None:
        if not (await self._ent.get(account_id)).allows("byok"):
            return None
        return await self._byok.resolve(account_id, provider)


@dataclass
class ProviderStack:
    providers: dict[str, ModelProvider]
    registry: ProviderRegistry
    catalog: ModelCatalog
    byok: ProviderCredentialService
    envelope: Envelope
    stub: StubProvider | None


def build_envelope(settings: Settings) -> Envelope:
    return envelope_from_settings(
        settings.master_key.get_secret_value() if settings.master_key else None,
        allow_ephemeral=settings.env in (Environment.DEV, Environment.TEST),
    )


def build_provider_stack(
    settings: Settings,
    *,
    db: Database,
    http: httpx.AsyncClient,
    entitlements: EntitlementService,
    byok_cache: TieredCache,
    provider_overrides: dict[str, ModelProvider] | None = None,
    credentials: CredentialResolver | None = None,
) -> ProviderStack:
    # Adapters are always registered: a tenant's own key (BYOK) can make a provider
    # usable even when AURA holds no platform key for it.
    timeout = settings.runtime_timeout_s
    providers: dict[str, ModelProvider] = {
        "anthropic": AnthropicProvider(timeout_s=timeout),
        "openai": OpenAICompatibleProvider("openai", http, timeout_s=timeout),
        "openrouter": OpenAICompatibleProvider("openrouter", http, timeout_s=timeout),
        "groq": OpenAICompatibleProvider("groq", http, timeout_s=timeout),
    }
    providers.update(provider_overrides or {})
    keys: dict[str, Credentials] = {}
    for name, secret in (
        ("anthropic", settings.anthropic_api_key),
        ("openai", settings.openai_api_key),
        ("openrouter", settings.openrouter_api_key),
        ("groq", settings.groq_api_key),
    ):
        if secret:
            keys[name] = Credentials(secret.get_secret_value())
    if settings.ollama_base_url:
        # Self-hosted models (Ollama or any keyless OpenAI-compatible server): free and
        # private, enabled only when an operator configures the URL.
        providers["ollama"] = OpenAICompatibleProvider("ollama", http, timeout_s=timeout)
        keys["ollama"] = Credentials("ollama", base_url=settings.ollama_base_url.rstrip("/"))
    stub: StubProvider | None = None
    if settings.stub_provider and settings.env in (Environment.DEV, Environment.TEST):
        existing = providers.get("stub")
        stub = existing if isinstance(existing, StubProvider) else StubProvider()
        providers["stub"] = stub
        keys["stub"] = STUB_CREDENTIALS
    envelope = build_envelope(settings)
    byok = ProviderCredentialService(db, envelope=envelope, cache=byok_cache)
    registry = ProviderRegistry(
        providers,
        credentials
        or ChainedCredentials(EntitledByok(byok, entitlements), PlatformCredentials(keys)),
    )
    catalog = load_model_catalog(enabled_providers=set(providers))
    return ProviderStack(providers, registry, catalog, byok, envelope, stub)
