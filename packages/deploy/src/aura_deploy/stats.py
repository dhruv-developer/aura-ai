"""Per-deployment variant statistics and business outcomes (Redis state role)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as aioredis

from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.redis import KeyFamily, build_key, ttl_for

MICRO = Decimal(1_000_000)  # cost stored in micro-USD integers (HINCRBY is integer)


@dataclass(frozen=True, slots=True)
class VariantSnapshot:
    n: int
    errors: int
    refusals: int
    cost_micro: int
    latency_ms_sum: int
    outcomes_ok: int
    outcomes_fail: int

    @property
    def error_rate(self) -> float:
        return self.errors / self.n if self.n else 0.0

    @property
    def avg_cost(self) -> float:
        ok = self.n - self.errors
        return self.cost_micro / ok / 1e6 if ok else 0.0

    @property
    def avg_latency_ms(self) -> float:
        ok = self.n - self.errors
        return self.latency_ms_sum / ok if ok else 0.0

    @property
    def outcomes(self) -> int:
        return self.outcomes_ok + self.outcomes_fail

    @property
    def success_rate(self) -> float:
        return self.outcomes_ok / self.outcomes if self.outcomes else 0.0

    @property
    def cost_per_outcome(self) -> float | None:
        """Spec §23/§59: AI cost divided by successful outcomes."""
        return (self.cost_micro / 1e6) / self.outcomes_ok if self.outcomes_ok else None


class VariantStats:
    def __init__(self, state: aioredis.Redis) -> None:
        self._r = state
        self._ttl = ttl_for(KeyFamily.DEPLOY_STATS, jitter=False) or 2_592_000
        self._map_ttl = ttl_for(KeyFamily.EXEC_VARIANT, jitter=False) or 604_800

    def _key(self, account_id: str, deployment_id: str, variant: str) -> str:
        return build_key(KeyFamily.DEPLOY_STATS, account_id, deployment_id, variant)

    def _map_key(self, account_id: str, execution_id: str) -> str:
        return build_key(KeyFamily.EXEC_VARIANT, account_id, execution_id)

    async def record_execution(
        self,
        *,
        account_id: str,
        execution_id: str,
        deployment_id: str | None,
        variant: str | None,
        policy_version_id: str | None,
        ok: bool,
        refused: bool,
        cost: Decimal,
        latency_ms: int,
    ) -> None:
        async with self._r.pipeline(transaction=False) as p:
            # Remember which policy produced this execution for later outcome reports.
            p.set(
                self._map_key(account_id, execution_id),
                json.dumps({"d": deployment_id, "v": variant, "p": policy_version_id}),
                ex=self._map_ttl,
            )
            if deployment_id and variant:
                key = self._key(account_id, deployment_id, variant)
                p.hincrby(key, "n", 1)
                if not ok:
                    p.hincrby(key, "errors", 1)
                if refused:
                    p.hincrby(key, "refusals", 1)
                if ok:
                    p.hincrby(key, "cost_micro", int(cost * MICRO))
                    p.hincrby(key, "latency_ms_sum", latency_ms)
                p.expire(key, self._ttl)
            await p.execute()

    async def record_outcome(
        self, account_id: str, execution_id: str, *, success: bool
    ) -> dict[str, str | None]:
        raw = await self._r.get(self._map_key(account_id, execution_id))
        if raw is None:
            # Unknown execution, another tenant's execution, or older than 7 days.
            raise NotFound("execution not found or outcome window expired")
        ref = json.loads(raw)
        if ref["d"] and ref["v"]:
            key = self._key(account_id, ref["d"], ref["v"])
            async with self._r.pipeline(transaction=False) as p:
                p.hincrby(key, "outcomes_ok" if success else "outcomes_fail", 1)
                p.expire(key, self._ttl)
                await p.execute()
        return {"deployment_id": ref["d"], "variant": ref["v"], "policy_version_id": ref["p"]}

    async def snapshot(self, account_id: str, deployment_id: str, variant: str) -> VariantSnapshot:
        raw = await self._r.hgetall(self._key(account_id, deployment_id, variant))
        v = {(k.decode() if isinstance(k, bytes) else k): int(x) for k, x in raw.items()}
        return VariantSnapshot(
            v.get("n", 0),
            v.get("errors", 0),
            v.get("refusals", 0),
            v.get("cost_micro", 0),
            v.get("latency_ms_sum", 0),
            v.get("outcomes_ok", 0),
            v.get("outcomes_fail", 0),
        )


class OutcomeService:
    """POST /v1/outcomes (spec §59): customers report business results per execution."""

    def __init__(self, stats: VariantStats) -> None:
        self._stats = stats

    async def record(
        self, account_id: str, execution_id: str, outcome: str
    ) -> dict[str, str | None]:
        if outcome not in ("success", "failure"):
            raise ValidationFailed("outcome must be success or failure")
        return await self._stats.record_outcome(
            account_id, execution_id, success=outcome == "success"
        )
