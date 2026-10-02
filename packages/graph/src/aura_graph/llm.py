"""LLM calls for graph nodes: per-node tier → router (policy-constrained) → provider
chain with fallback. Cost is computed from the served model."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from aura_policy import ExecutionPolicy, QualityMode, merge
from aura_policy.pii import apply_pii_policy
from aura_providers.domain.catalog import ModelCatalog
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.tokens import estimate_input_tokens
from aura_providers.domain.types import ChatRequest, Message, ProviderError, ProviderErrorKind
from aura_providers.registry import ProviderRegistry
from aura_routing import Router
from aura_shared.ids import new_id

TIER_QUALITY = {
    "small": QualityMode.FAST,
    "medium": QualityMode.BALANCED,
    "frontier": QualityMode.BEST,
    "auto": QualityMode.AUTO,
}
ESCALATE = {"small": "medium", "auto": "medium", "medium": "frontier", "frontier": "frontier"}


@dataclass(frozen=True, slots=True)
class LlmResult:
    text: str
    cost: Decimal
    model: str
    latency_ms: int
    attempts: int


class LlmCaller(Protocol):
    async def call(
        self,
        account_id: str,
        *,
        tier: str,
        messages: list[Message],
        policy: ExecutionPolicy,
        max_tokens: int = 2_000,
        region: str = "global",
        model: str | None = None,
    ) -> LlmResult: ...


class CreditGate(Protocol):
    async def exhausted(self, account_id: str) -> bool: ...


class CreditMeter(Protocol):
    async def charge(
        self, *, account_id: str, execution_id: str, cost_usd: Decimal, credential_source: str
    ) -> int: ...


class RoutedLlm:
    """Consumer credit rules match the single-step engine: an exhausted account may only
    use its own keys, and platform-key calls are charged per call."""

    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        router: Router,
        registry: ProviderRegistry,
        credit_gate: CreditGate | None = None,
        credit_meter: CreditMeter | None = None,
    ) -> None:
        self._catalog, self._router, self._registry = catalog, router, registry
        self._gate, self._meter = credit_gate, credit_meter

    async def _usable(self, account_id: str) -> set[str]:
        if self._gate is None or not await self._gate.exhausted(account_id):
            return await self._registry.usable_providers(account_id)
        sources = await self._registry.credential_sources(account_id)
        own = {name for name, source in sources.items() if source != "platform"}
        if not own:
            raise ProviderError(ProviderErrorKind.RATE_LIMITED, "out of credits")
        return own

    async def call(
        self,
        account_id: str,
        *,
        tier: str,
        messages: list[Message],
        policy: ExecutionPolicy,
        max_tokens: int = 2_000,
        region: str = "global",
        model: str | None = None,
    ) -> LlmResult:
        """``model`` pins one specific model (e.g. the fixed reviewer) when it is usable;
        otherwise the call is routed normally (the result's ``model`` shows which)."""
        req = ChatRequest(tuple(messages))
        # Same personal-data rule as single-step executions (raises PiiBlocked on BLOCK).
        req, _ = apply_pii_policy(req, policy.effective_pii)
        tier_policy = ExecutionPolicy(quality=TIER_QUALITY.get(tier, QualityMode.AUTO))
        effective = (
            merge(tier_policy, policy) if policy.quality is None else merge(policy, tier_policy)
        )
        usable = await self._usable(account_id)
        exclude = frozenset(m.id for m in self._catalog.enabled() if m.provider not in usable)
        pinned = self._catalog.get(model) if model else None
        explicit = model if pinned is not None and model not in exclude else None
        route = self._router.route(
            req,
            effective,
            est_input_tokens=estimate_input_tokens(req),
            requested_max_tokens=max_tokens,
            explicit_model=explicit,
            exclude=exclude,
            region=region,
        )
        last: ProviderError | None = None
        for attempt, spec in enumerate(route.chain, 1):
            t0 = time.monotonic()
            try:
                provider = self._registry.provider_for(spec)
                creds = await self._registry.credentials_for(account_id, spec)
                r = await provider.complete(spec, req, creds, max_tokens=route.max_tokens[spec.id])
            except ProviderError as exc:
                last = exc
                if not exc.retryable:
                    raise
                continue
            cost = cost_usd(spec, r.usage)
            if self._meter is not None and cost > 0:
                await self._meter.charge(
                    account_id=account_id,
                    execution_id=new_id("llm"),
                    cost_usd=cost,
                    credential_source=creds.source,
                )
            return LlmResult(
                r.text,
                cost,
                spec.id,
                int((time.monotonic() - t0) * 1000),
                attempt,
            )
        raise last or ProviderError(ProviderErrorKind.UNAVAILABLE, "no model available")
