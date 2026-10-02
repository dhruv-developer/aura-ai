"""Model catalog. Pure apart from reading the YAML."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


class Tier(IntEnum):
    SMALL = 1
    MEDIUM = 2
    FRONTIER = 3


KNOWN_CAPABILITIES = frozenset(
    {
        "sampling",  # accepts temperature (Opus 5.5 / Sonnet 5.5 reject it)
        "effort",  # accepts output_config.effort
        "refusal_fallback",  # Anthropic server-side fallbacks: "default"
        "vision",
        "stop_sequences",
    }
)


class CatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per 1M tokens."""

    input: Decimal
    output: Decimal
    cached_input: Decimal
    cache_write: Decimal


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str  # AURA id: "<provider>/<upstream>"
    provider: str
    upstream: str
    display_name: str
    tier: Tier
    context_window: int
    max_output: int
    prices: Prices
    capabilities: frozenset[str]
    regions: tuple[str, ...]
    enabled: bool = True

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    models: dict[str, ModelSpec]
    prices_as_of: str
    version: str  # content hash: recorded on every execution for cost reproducibility

    def get(self, model_id: str) -> ModelSpec | None:
        return self.models.get(model_id)

    def resolve(self, name: str) -> ModelSpec | None:
        """Accept an AURA id (``anthropic/claude-sonnet-5-5``) or a bare upstream id
        (``claude-sonnet-5-5``) when that upstream id is unambiguous."""
        if spec := self.models.get(name):
            return spec
        matches = [m for m in self.models.values() if m.upstream == name]
        return matches[0] if len(matches) == 1 else None

    def enabled(self) -> list[ModelSpec]:
        return [m for m in self.models.values() if m.enabled]


def _dec(value: Any, where: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception as exc:
        raise CatalogError(f"{where}: not a number: {value!r}") from exc
    if d < 0:
        raise CatalogError(f"{where}: price must be >= 0")
    return d


def parse_catalog(
    data: dict[str, Any], *, enabled_providers: set[str] | None = None
) -> ModelCatalog:
    models: dict[str, ModelSpec] = {}
    for provider, block in (data.get("providers") or {}).items():
        for upstream, spec in (block.get("models") or {}).items():
            mid = f"{provider}/{upstream}"
            caps = frozenset(spec.get("capabilities") or [])
            if unknown := caps - KNOWN_CAPABILITIES:
                raise CatalogError(f"{mid}: unknown capabilities {sorted(unknown)}")
            try:
                tier = Tier[str(spec["tier"]).upper()]
            except KeyError as exc:
                raise CatalogError(f"{mid}: tier must be small|medium|frontier") from exc
            p = spec["prices"]
            inp = _dec(p["input"], f"{mid}.input")
            prices = Prices(
                input=inp,
                output=_dec(p["output"], f"{mid}.output"),
                cached_input=_dec(p.get("cached_input", p["input"]), f"{mid}.cached_input"),
                cache_write=_dec(p.get("cache_write", p["input"]), f"{mid}.cache_write"),
            )
            ctx, out = int(spec["context_window"]), int(spec["max_output"])
            if ctx <= 0 or out <= 0 or out > ctx:
                raise CatalogError(f"{mid}: invalid context_window/max_output")
            models[mid] = ModelSpec(
                id=mid,
                provider=provider,
                upstream=upstream,
                display_name=spec.get("display_name", upstream),
                tier=tier,
                context_window=ctx,
                max_output=out,
                prices=prices,
                capabilities=caps,
                regions=tuple(spec.get("regions") or ["global"]),
                enabled=bool(spec.get("enabled", True))
                and (enabled_providers is None or provider in enabled_providers),
            )
    if not models:
        raise CatalogError("catalog has no models")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    return ModelCatalog(models, str(data.get("prices_as_of", "unknown")), digest[:16])


def load_catalog(
    path: Path | None = None, *, enabled_providers: set[str] | None = None
) -> ModelCatalog:
    text = (
        path.read_text("utf-8")
        if path
        else (resources.files("aura_providers") / "models.yaml").read_text("utf-8")
    )
    return parse_catalog(yaml.safe_load(text), enabled_providers=enabled_providers)
