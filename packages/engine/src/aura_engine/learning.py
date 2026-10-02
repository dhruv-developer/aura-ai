"""Learned routing (spec §112, §168): LinUCB chooses among the models the rules router
already found eligible. Rules always win; the bandit only reorders a constrained chain.

State is one Redis value per (account, workload), updated with optimistic concurrency
(WATCH/MULTI). A capped decision log feeds off-policy evaluation.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import WatchError

from aura_learning import FEATURE_DIM, LinUCB, LoggedDecision, feature_vector
from aura_providers.domain.catalog import ModelSpec
from aura_routing import Route
from aura_shared.events import RoutingDecision
from aura_shared.redis import KeyFamily, build_key

log = logging.getLogger("aura.engine.learning")
ROUTER_VERSION = "linucb-v1"
DECISION_LOG_CAP = 10_000


@dataclass(frozen=True, slots=True)
class LearnedChoice:
    workload: str
    arm: str
    context: list[float]
    propensity: float


def _slug(workload: str | None) -> str:
    raw = (workload or "_default").lower()
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in raw)[:80] or "_default"


class LearnedSelector:
    def __init__(
        self,
        state: aioredis.Redis,
        *,
        epsilon: float = 0.05,
        daily_explore_budget: int = 500,
        alpha: float = 0.5,
        rng: random.Random | None = None,
    ) -> None:
        self._r, self.epsilon, self._budget, self._alpha = (
            state,
            epsilon,
            daily_explore_budget,
            alpha,
        )
        self._rng = rng or random.Random()  # noqa: S311 - exploration, not crypto

    def _key(self, account_id: str, workload: str, part: str) -> str:
        return build_key(KeyFamily.LEARNING, account_id, _slug(workload), part)

    async def _load(self, account_id: str, workload: str) -> LinUCB:
        raw = await self._r.get(self._key(account_id, workload, "bandit"))
        if raw is None:
            bandit = LinUCB(FEATURE_DIM, alpha=self._alpha)
            prior = await self._r.get(build_key(KeyFamily.LEARNING, "_global", "prior"))
            if prior is not None:  # consent-gated aggregate warm start
                for arm, stats in json.loads(prior).items():
                    bandit.warm_start(
                        arm, mean_reward=stats["mean"], pseudo_count=min(stats["n"], 20)
                    )
            return bandit
        return LinUCB.from_dict(json.loads(raw))

    async def _explore_allowed(self, account_id: str) -> bool:
        key = build_key(
            KeyFamily.LEARNING, account_id, "explore", datetime.now(UTC).strftime("%Y%m%d")
        )
        used = await self._r.incr(key)
        if used == 1:
            await self._r.expire(key, 2 * 86_400)
        return int(used) <= self._budget

    async def choose(
        self, account_id: str, workload: str | None, route: Route
    ) -> tuple[Route, LearnedChoice]:
        arms = [s.id for s in route.chain]
        x = feature_vector(route.decision.features)
        bandit = await self._load(account_id, workload or "_default")
        epsilon = self.epsilon if len(arms) > 1 and await self._explore_allowed(account_id) else 0.0
        arm, propensity = bandit.choose(x, arms, epsilon=epsilon, rng=self._rng)
        chosen: ModelSpec = next(s for s in route.chain if s.id == arm)
        rest = tuple(s for s in route.chain if s.id != arm)
        decision = RoutingDecision(
            router_version=ROUTER_VERSION,
            chosen_model=arm,
            candidates=[arm, *[s.id for s in rest]],
            features=route.decision.features,
            propensity=round(propensity, 6),
            reason=f"{route.decision.reason}; learned: {arm} (ε={epsilon})",
        )
        new_route = Route(
            chosen, rest, route.max_tokens, decision, route.target_tier, route.est_input_tokens
        )
        return new_route, LearnedChoice(workload or "_default", arm, x, propensity)

    async def update(self, account_id: str, choice: LearnedChoice, reward: float) -> None:
        key = self._key(account_id, choice.workload, "bandit")
        for _ in range(8):
            try:
                async with self._r.pipeline(transaction=True) as p:
                    await p.watch(key)
                    raw = await p.get(key)
                    bandit = (
                        LinUCB.from_dict(json.loads(raw))
                        if raw
                        else await self._load(account_id, choice.workload)
                    )
                    bandit.update(choice.arm, choice.context, reward)
                    p.multi()  # type: ignore[no-untyped-call]  # redis stubs
                    p.set(key, json.dumps(bandit.to_dict()))
                    await p.execute()
                break
            except WatchError:
                continue
        else:
            log.warning("bandit update lost after retries (high contention)")
        log_key = self._key(account_id, choice.workload, "log")
        entry = json.dumps(
            {"x": choice.context, "a": choice.arm, "p": choice.propensity, "r": reward}
        )
        async with self._r.pipeline(transaction=False) as p:
            p.rpush(log_key, entry)
            p.ltrim(log_key, -DECISION_LOG_CAP, -1)
            await p.execute()

    async def decisions(
        self, account_id: str, workload: str | None, *, limit: int = 5_000
    ) -> list[LoggedDecision]:
        raw = await self._r.lrange(self._key(account_id, workload or "_default", "log"), -limit, -1)
        out = []
        for item in raw:
            d = json.loads(item)
            out.append(LoggedDecision(tuple(d["x"]), d["a"], float(d["p"]), float(d["r"])))
        return out

    async def bandit(self, account_id: str, workload: str | None) -> LinUCB:
        return await self._load(account_id, workload or "_default")

    async def publish_prior(self, stats: dict[str, dict[str, Any]]) -> None:
        """Aggregated per-arm reward means from *consenting* tenants only (no content)."""
        await self._r.set(build_key(KeyFamily.LEARNING, "_global", "prior"), json.dumps(stats))


async def aggregate_prior(
    state: aioredis.Redis, consenting_account_ids: list[str], *, env: str
) -> dict[str, dict[str, Any]]:
    """Per-arm mean reward across the decision logs of *consenting* tenants only. It holds
    aggregate numbers, never content. Tenants without consent contribute nothing."""
    totals: dict[str, list[float]] = {}
    for account_id in consenting_account_ids:
        pattern = f"aura:{env}:learn:{{{account_id}}}:*:log:v1"
        async for key in state.scan_iter(match=pattern, count=200):
            for item in await state.lrange(key, -2_000, -1):
                d = json.loads(item)
                totals.setdefault(d["a"], []).append(float(d["r"]))
    return {arm: {"mean": sum(v) / len(v), "n": len(v)} for arm, v in totals.items() if v}
