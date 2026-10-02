"""Reward = quality - cost_weight * normalized cost - latency_weight * normalized latency
(spec §154: minimise cost subject to quality). Pure."""

from __future__ import annotations


def reward(
    *,
    success: bool,
    cost_usd: float,
    cost_ref_usd: float,
    latency_ms: float = 0.0,
    latency_ref_ms: float = 10_000.0,
    cost_weight: float = 0.3,
    latency_weight: float = 0.05,
) -> float:
    quality = 1.0 if success else 0.0
    cost_term = min(cost_usd / cost_ref_usd, 2.0) if cost_ref_usd > 0 else 0.0
    latency_term = min(latency_ms / latency_ref_ms, 2.0)
    return quality - cost_weight * cost_term - latency_weight * latency_term
