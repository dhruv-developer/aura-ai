"""LinUCB contextual bandit with ε-bounded exploration. Pure Python, small dimensions.

The inverse design matrix is maintained with Sherman-Morrison rank-1 updates (O(d²) per
update, no matrix inversion). Selection mixes the UCB argmax with ε-uniform exploration,
so every logged decision has a *true* propensity, which off-policy evaluation requires.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

Vector = list[float]
Matrix = list[list[float]]


def _identity(d: int, scale: float) -> Matrix:
    return [[scale if i == j else 0.0 for j in range(d)] for i in range(d)]


def _matvec(m: Matrix, v: Sequence[float]) -> Vector:
    return [sum(r[j] * v[j] for j in range(len(v))) for r in m]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class LinUCB:
    def __init__(self, dim: int, *, alpha: float = 1.0, l2: float = 1.0) -> None:
        self.dim, self.alpha, self.l2 = dim, alpha, l2
        self.a_inv: dict[str, Matrix] = {}
        self.b: dict[str, Vector] = {}
        self.n: dict[str, int] = {}

    def _ensure(self, arm: str) -> None:
        if arm not in self.a_inv:
            self.a_inv[arm] = _identity(self.dim, 1.0 / self.l2)
            self.b[arm] = [0.0] * self.dim
            self.n[arm] = 0

    def theta(self, arm: str) -> Vector:
        self._ensure(arm)
        return _matvec(self.a_inv[arm], self.b[arm])

    def ucb(self, arm: str, x: Sequence[float]) -> float:
        self._ensure(arm)
        ainv_x = _matvec(self.a_inv[arm], x)
        return _dot(self.theta(arm), x) + self.alpha * math.sqrt(max(_dot(x, ainv_x), 0.0))

    def choose(
        self, x: Sequence[float], arms: Sequence[str], *, epsilon: float, rng: random.Random
    ) -> tuple[str, float]:
        """Returns (arm, propensity of that arm under this selection rule)."""
        if not arms:
            raise ValueError("no arms")
        best = max(arms, key=lambda a: (self.ucb(a, x), -arms.index(a)))
        explore = rng.random() < epsilon
        arm = rng.choice(list(arms)) if explore else best
        k = len(arms)
        propensity = (1 - epsilon) * (1.0 if arm == best else 0.0) + epsilon / k
        return arm, propensity

    def probabilities(
        self, x: Sequence[float], arms: Sequence[str], *, epsilon: float
    ) -> dict[str, float]:
        best = max(arms, key=lambda a: (self.ucb(a, x), -arms.index(a)))
        return {a: (1 - epsilon) * (a == best) + epsilon / len(arms) for a in arms}

    def update(self, arm: str, x: Sequence[float], reward: float) -> None:
        self._ensure(arm)
        ainv = self.a_inv[arm]
        ainv_x = _matvec(ainv, x)
        denom = 1.0 + _dot(x, ainv_x)
        d = self.dim
        self.a_inv[arm] = [
            [ainv[i][j] - ainv_x[i] * ainv_x[j] / denom for j in range(d)] for i in range(d)
        ]
        self.b[arm] = [bi + reward * xi for bi, xi in zip(self.b[arm], x, strict=True)]
        self.n[arm] += 1

    def warm_start(self, arm: str, *, mean_reward: float, pseudo_count: int) -> None:
        """Prior from aggregated (consenting) tenants: pseudo-observations on the bias."""
        bias = [1.0] + [0.0] * (self.dim - 1)
        for _ in range(max(pseudo_count, 0)):
            self.update(arm, bias, mean_reward)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "alpha": self.alpha,
            "l2": self.l2,
            "a_inv": self.a_inv,
            "b": self.b,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LinUCB:
        m = cls(int(d["dim"]), alpha=float(d["alpha"]), l2=float(d["l2"]))
        m.a_inv, m.b, m.n = d["a_inv"], d["b"], {k: int(v) for k, v in d["n"].items()}
        return m
