"""Normalized traffic records and price lookup. Pure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aura_providers.domain.catalog import ModelCatalog, ModelSpec, Tier
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.types import Usage


@dataclass(frozen=True, slots=True)
class TrafficRecord:
    ts: datetime | None
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    latency_ms: int | None = None
    cost_usd: Decimal | None = None  # as reported by the source, if any
    status: str = "ok"  # ok | error
    finish_reason: str | None = None
    system_prompt: str | None = None
    user_content: str | None = None
    workload_hint: str | None = None
    response_text: str | None = None  # in memory only (shadow baselines); never persisted
    features: dict[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PriceBook:
    """Prices from the AURA catalog, plus caller-supplied overrides (USD/MTok) for models
    AURA does not route but customers use."""

    catalog: ModelCatalog
    overrides: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)

    def spec(self, model: str) -> ModelSpec | None:
        return self.catalog.resolve(model)

    def cost(self, rec: TrafficRecord) -> Decimal | None:
        if rec.cost_usd is not None:
            return rec.cost_usd
        if model_override := self.overrides.get(rec.model):
            inp, out = model_override
            return (rec.input_tokens * inp + rec.output_tokens * out) / Decimal(1_000_000)
        spec = self.spec(rec.model)
        if spec is None:
            return None
        return cost_usd(
            spec,
            Usage(rec.input_tokens, rec.output_tokens, cached_input_tokens=rec.cached_input_tokens),
        )

    def cheapest(self, tier: Tier) -> ModelSpec | None:
        candidates = [
            m for m in self.catalog.models.values() if m.tier is tier and m.provider != "stub"
        ]
        return min(candidates, key=lambda m: m.prices.input * 3 + m.prices.output, default=None)

    def tier_of(self, model: str) -> Tier | None:
        spec = self.spec(model)
        return spec.tier if spec else None
