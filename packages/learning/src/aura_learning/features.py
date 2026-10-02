"""Routing features → a fixed-length context vector. Pure."""

from __future__ import annotations

import math
from collections.abc import Mapping

FEATURE_DIM = 8


def feature_vector(f: Mapping[str, float | int | str | bool]) -> list[float]:
    def num(key: str, default: float = 0.0) -> float:
        v = f.get(key, default)
        return float(v) if isinstance(v, int | float) else default

    return [
        1.0,  # bias
        num("complexity", 0.3),
        min(math.log1p(num("est_input_tokens")) / 10, 1.5),
        1.0 if f.get("has_code") else 0.0,
        1.0 if f.get("has_math") else 0.0,
        min(num("reasoning_cues") / 3, 1.0),
        num("non_ascii_ratio"),
        min(num("n_messages") / 10, 1.0),
    ]
