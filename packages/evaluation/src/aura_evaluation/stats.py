"""Confidence intervals. Pure, deterministic (seeded) where randomness is involved."""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion: well-behaved at small n and near 0 or 1,
    unlike the normal approximation."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_ci(
    values: Sequence[float],
    *,
    stat: Callable[[Sequence[float]], float] = lambda v: sum(v) / len(v),
    iterations: int = 2_000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311 - statistics, not crypto
    n = len(values)
    samples = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(iterations))
    lo = samples[int(alpha / 2 * iterations)]
    hi = samples[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return lo, hi
