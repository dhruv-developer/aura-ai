"""Autopilot (spec §168): continuous optimization without a human in the loop for
*proven* changes:

    completed shadow run (gate passed) → workload policy pinned to the proven model
    → canary deployment with that run as evidence → the canary monitor promotes
    or rolls back

It never forces past the safety gate, and never touches an in-flight canary.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from aura_db import Database
from aura_db.models import Account, ExecutionPolicyRow, ShadowRun
from aura_deploy.recommendations import workload_ref
from aura_deploy.service import PolicyService
from aura_shared.errors import AuraError, Conflict

log = logging.getLogger("aura.deploy.autopilot")
SYSTEM_ACTOR = "autopilot"


class AutopilotService:
    def __init__(self, db: Database, *, policies: PolicyService) -> None:
        self._db = db
        self._policies = policies

    async def tick(self) -> list[dict[str, Any]]:
        async with self._db.system() as s:
            accounts = list(
                await s.scalars(
                    select(Account.id).where(
                        Account.autopilot.is_(True), Account.status == "active"
                    )
                )
            )
        actions = []
        for account_id in accounts:
            try:
                actions.extend(await self.run_for(account_id))
            except Exception:
                log.exception("autopilot failed for %s", account_id)
        return actions

    async def run_for(self, account_id: str) -> list[dict[str, Any]]:
        async with self._db.tenant(account_id) as s:
            runs = [
                r
                for r in await s.scalars(
                    select(ShadowRun)
                    .where(ShadowRun.status == "completed")
                    .order_by(ShadowRun.created_at)
                )
                if not r.results.get("autopilot") and r.config.get("workload_key")
            ]
        actions = []
        for run in runs:
            proven = [c for c in run.results.get("candidates", []) if c.get("deploy_recommended")]
            if not proven:
                await self._mark(account_id, run.id, {"skipped": "no recommended candidate"})
                continue
            best = proven[0]  # results are sorted: recommended first, biggest saving first
            ref = workload_ref(run.config["workload_key"], run.config.get("workload", ""))
            spec = {"allowed_models": [best["model"]]}
            try:
                policy_id, version_id = await self._version(account_id, ref, spec)
                dep = await self._policies.deploy(
                    account_id=account_id,
                    policy_id=policy_id,
                    version_id=version_id,
                    strategy="canary",
                    actor_id=SYSTEM_ACTOR,
                    shadow_run_id=run.id,
                )
            except Conflict:
                continue  # a canary is already in flight for this workload: retry next tick
            except AuraError as exc:
                await self._mark(account_id, run.id, {"skipped": exc.detail})
                continue
            await self._mark(account_id, run.id, {"deployment_id": dep["deployment_id"]})
            actions.append(
                {
                    "account_id": account_id,
                    "shadow_run_id": run.id,
                    "workload": ref,
                    "model": best["model"],
                    **dep,
                }
            )
        return actions

    async def _version(self, account_id: str, ref: str, spec: dict[str, Any]) -> tuple[str, str]:
        async with self._db.tenant(account_id) as s:
            pol = await s.scalar(
                select(ExecutionPolicyRow).where(
                    ExecutionPolicyRow.scope == "workload", ExecutionPolicyRow.scope_ref == ref
                )
            )
        if pol is None:
            created = await self._policies.create_policy(
                account_id=account_id,
                name=f"autopilot: {ref}",
                scope="workload",
                scope_ref=ref,
                spec=spec,
                actor_id=SYSTEM_ACTOR,
            )
            return created["policy_id"], created["version_id"]
        vid = await self._policies.add_version(
            account_id=account_id,
            policy_id=pol.id,
            spec=spec,
            note="autopilot",
            actor_id=SYSTEM_ACTOR,
        )
        return pol.id, vid

    async def _mark(self, account_id: str, run_id: str, info: dict[str, Any]) -> None:
        async with self._db.tenant(account_id) as s:
            run = await s.get(ShadowRun, run_id)
            if run is not None:
                run.results = {**run.results, "autopilot": info}
