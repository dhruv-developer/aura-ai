"""Off-policy evaluation (spec §112): estimate a *new* policy's value from decisions
logged under the old one, before any traffic moves. Pure.

- IPS: unbiased, high variance.
- SNIPS: self-normalized, lower variance, small bias.
- Doubly robust: unbiased if either the propensities or the reward model are right.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoggedDecision:
    context: tuple[float, ...]
    action: str
    propensity: float  # probability the logging policy chose ``action``
    reward: float


TargetPolicy = Callable[[tuple[float, ...]], dict[str, float]]  # context → action probs


def _estimates(logs: Sequence[LoggedDecision], target: TargetPolicy) -> dict[str, float]:
    n = len(logs)
    by_action: dict[str, list[float]] = defaultdict(list)
    for d in logs:
        by_action[d.action].append(d.reward)
    q = {a: sum(v) / len(v) for a, v in by_action.items()}  # simple per-action reward model
    ips = snips_num = snips_den = dr = 0.0
    for d in logs:
        probs = target(d.context)
        w = probs.get(d.action, 0.0) / max(d.propensity, 1e-6)
        ips += w * d.reward
        snips_num += w * d.reward
        snips_den += w
        direct = sum(p * q.get(a, 0.0) for a, p in probs.items())
        dr += direct + w * (d.reward - q.get(d.action, 0.0))
    return {
        "ips": ips / n,
        "snips": snips_num / snips_den if snips_den else 0.0,
        "dr": dr / n,
    }


def estimate(
    logs: Sequence[LoggedDecision], target: TargetPolicy, *, bootstrap: int = 500, seed: int = 7
) -> dict[str, object]:
    if not logs:
        raise ValueError("no logged decisions")
    point = _estimates(logs, target)
    rng = random.Random(seed)  # noqa: S311 - statistics
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(bootstrap):
        resample = [logs[rng.randrange(len(logs))] for _ in logs]
        for k, v in _estimates(resample, target).items():
            samples[k].append(v)
    ci = {
        k: (sorted(v)[int(0.025 * len(v))], sorted(v)[min(len(v) - 1, int(0.975 * len(v)))])
        for k, v in samples.items()
    }
    logged_value = sum(d.reward for d in logs) / len(logs)
    return {"estimates": point, "ci95": ci, "logging_policy_value": logged_value, "n": len(logs)}
