"""Canary monitor: advance healthy canaries stage by stage; roll back regressions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aura_db import Database
from aura_db.models import Deployment
from aura_deploy.service import PolicyService
from aura_deploy.stats import VariantSnapshot, VariantStats

log = logging.getLogger("aura.deploy.canary")
SYSTEM = ("system", "canary-monitor")


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    min_samples: int = 50  # per stage, canary side
    min_dwell: timedelta = timedelta(minutes=10)
    max_error_increase: float = 0.02  # absolute
    max_latency_ratio: float = 1.3
    max_cost_ratio: float = 1.10  # canaries exist to save money; a cost regression fails
    min_outcomes: int = 30
    max_success_drop: float = 0.05  # absolute, when both sides have outcomes


def assess(
    canary: VariantSnapshot, stable: VariantSnapshot, cfg: CanaryConfig
) -> tuple[str, list[str]]:
    """Returns (decision, reasons): decision is wait | rollback | advance. Pure."""
    if canary.n < cfg.min_samples:
        return "wait", [f"canary has {canary.n}/{cfg.min_samples} samples"]
    reasons = []
    if (
        canary.error_rate - stable.error_rate > cfg.max_error_increase
        and canary.error_rate > stable.error_rate * 1.5
    ):
        reasons.append(f"error rate {canary.error_rate:.3f} vs stable {stable.error_rate:.3f}")
    if stable.n >= cfg.min_samples and stable.avg_latency_ms > 0:
        if canary.avg_latency_ms > stable.avg_latency_ms * cfg.max_latency_ratio:
            reasons.append(
                f"latency {canary.avg_latency_ms:.0f}ms vs {stable.avg_latency_ms:.0f}ms"
            )
        if stable.avg_cost > 0 and canary.avg_cost > stable.avg_cost * cfg.max_cost_ratio:
            reasons.append(f"cost/request {canary.avg_cost:.6f} vs {stable.avg_cost:.6f}")
    both_have_outcomes = canary.outcomes >= cfg.min_outcomes and stable.outcomes >= cfg.min_outcomes
    if both_have_outcomes and stable.success_rate - canary.success_rate > cfg.max_success_drop:
        reasons.append(f"outcome success {canary.success_rate:.3f} vs {stable.success_rate:.3f}")
    return ("rollback", reasons) if reasons else ("advance", ["healthy"])


class CanaryMonitor:
    def __init__(
        self,
        db: Database,
        *,
        policies: PolicyService,
        stats: VariantStats,
        config: CanaryConfig | None = None,
    ) -> None:
        self._db = db
        self._policies = policies
        self._stats = stats
        self.cfg = config or CanaryConfig()

    async def run_once(self) -> list[dict[str, Any]]:
        async with self._db.system() as s:  # cross-tenant sweep, by design
            deps = list(
                await s.scalars(
                    select(Deployment).where(
                        Deployment.status == "canary", Deployment.mode == "canary"
                    )
                )
            )
        actions = []
        for d in deps:
            try:
                actions.append(await self.evaluate(d.account_id, d.id, d.stage_started_at))
            except Exception:
                log.exception("canary evaluation failed for %s", d.id)
        return actions

    async def evaluate(
        self, account_id: str, deployment_id: str, stage_started_at: datetime
    ) -> dict[str, Any]:
        canary = await self._stats.snapshot(account_id, deployment_id, "canary")
        stable = await self._stats.snapshot(account_id, deployment_id, "stable")
        decision, reasons = assess(canary, stable, self.cfg)
        if decision == "advance" and datetime.now(UTC) - stage_started_at < self.cfg.min_dwell:
            decision, reasons = "wait", ["minimum dwell time not reached"]
        if decision == "rollback":
            await self._policies.rollback(
                account_id=account_id,
                deployment_id=deployment_id,
                reason="automatic: " + "; ".join(reasons),
                actor=SYSTEM,
            )
        elif decision == "advance":
            await self._policies.advance(
                account_id=account_id, deployment_id=deployment_id, actor=SYSTEM
            )
        return {"deployment_id": deployment_id, "decision": decision, "reasons": reasons}
