"""Engine observers wired by the runtime (app layer composes engine + deploy)."""

from __future__ import annotations

from decimal import Decimal

from aura_billing.application.consumer import CreditMeter
from aura_deploy import VariantStats
from aura_engine.engine import Execution, PreparedExecution
from aura_engine.learning import LearnedSelector
from aura_learning import reward
from aura_providers.domain.types import FinishReason


class DeployObserver:
    """Feeds canary statistics and the execution→variant map used by /v1/outcomes."""

    def __init__(self, stats: VariantStats) -> None:
        self._stats = stats

    async def on_finished(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None:
        refused = execution is not None and (
            execution.result.finish_reason is FinishReason.CONTENT_FILTER
        )
        await self._stats.record_execution(
            account_id=run.ctx.account_id,
            execution_id=run.execution_id,
            deployment_id=run.opts.deployment_id,
            variant=run.opts.variant,
            policy_version_id=run.opts.policy_version_id,
            ok=ok,
            refused=refused,
            cost=execution.cost_usd if execution else Decimal(0),
            latency_ms=execution.latency_ms if execution else 0,
        )


class LearningObserver:
    """Rewards each learned routing decision at completion: success minus a cost penalty.
    Business outcomes (POST /v1/outcomes) add a second, outcome-based observation."""

    def __init__(self, learner: LearnedSelector, *, cost_ref_usd: float = 0.01) -> None:
        self._learner, self._cost_ref = learner, cost_ref_usd

    async def on_finished(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None:
        if run.learning is None:
            return
        success = (
            ok
            and execution is not None
            and execution.result.finish_reason is FinishReason.STOP
            and execution.spec.id == run.learning.arm  # a fallback served it: the arm failed
        )
        r = reward(
            success=success,
            cost_usd=float(execution.cost_usd) if execution else 0.0,
            cost_ref_usd=self._cost_ref,
            latency_ms=float(execution.latency_ms) if execution else 0.0,
        )
        await self._learner.update(run.ctx.account_id, run.learning, r)


class CreditObserver:
    """Charges consumer accounts for platform-key inference (idempotent per execution)."""

    def __init__(self, meter: CreditMeter) -> None:
        self._meter = meter

    async def on_finished(
        self, run: PreparedExecution, execution: Execution | None, *, ok: bool
    ) -> None:
        if execution is None or execution.cost_usd <= 0:
            return
        await self._meter.charge(
            account_id=run.ctx.account_id,
            execution_id=run.execution_id,
            cost_usd=execution.cost_usd,
            credential_source=execution.credential_source,
        )
