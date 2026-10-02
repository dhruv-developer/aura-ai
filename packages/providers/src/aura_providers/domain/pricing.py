"""Cost computation. Pure. All money is Decimal USD; never floats."""

from __future__ import annotations

from decimal import Decimal

from aura_providers.domain.catalog import ModelSpec
from aura_providers.domain.types import Usage

MTOK = Decimal(1_000_000)
USD_QUANTUM = Decimal("0.00000001")  # 1e-8 USD; enough for single-token costs


def cost_usd(spec: ModelSpec, usage: Usage) -> Decimal:
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    written = min(usage.cache_write_tokens, usage.input_tokens - cached)
    plain = usage.input_tokens - cached - written
    p = spec.prices
    total = (
        plain * p.input
        + cached * p.cached_input
        + written * p.cache_write
        + usage.output_tokens * p.output
    ) / MTOK
    return total.quantize(USD_QUANTUM)


def estimate_cost_usd(spec: ModelSpec, *, input_tokens: int, output_tokens: int) -> Decimal:
    """Upper-bound style estimate used before execution (no cache discount assumed)."""
    return cost_usd(spec, Usage(input_tokens=input_tokens, output_tokens=output_tokens))
