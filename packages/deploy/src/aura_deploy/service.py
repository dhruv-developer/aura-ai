"""Policies, immutable versions, and evidence-gated deployments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aura_billing.application.entitlements import EntitlementService
from aura_cache import TieredCache
from aura_db import Database, audit, outbox
from aura_db.models import Deployment, ExecutionPolicyRow, PolicyVersion, ShadowRun
from aura_policy import PolicySpecError, from_dict, to_dict
from aura_shared.errors import Conflict, Forbidden, NotFound, ValidationFailed
from aura_shared.ids import new_id

SCOPES = ("account", "workload")
CANARY_STAGES = (1, 5, 10, 25, 50, 100)


@dataclass(frozen=True, slots=True)
class Evidence:
    ok: bool
    reasons: list[str]
    detail: dict[str, Any]


def check_evidence(spec: dict[str, Any], run: ShadowRun | None) -> Evidence:
    """A version is proven only if it routes *exclusively* to models that a completed
    shadow run recommended (gate passed). A policy that allows unproven models could
    send traffic to models nobody verified."""
    if run is None:
        return Evidence(False, ["no shadow run evidence provided"], {})
    if run.status != "completed":
        return Evidence(
            False, [f"shadow run is {run.status}, not completed"], {"shadow_run_id": run.id}
        )
    proven = {c["model"] for c in run.results.get("candidates", []) if c.get("deploy_recommended")}
    allowed = set(spec.get("allowed_models") or [])
    reasons = []
    if not proven:
        reasons.append("the shadow run recommended no candidate (quality gate not met)")
    if not allowed:
        reasons.append("policy must pin allowed_models to the proven candidates")
    elif not allowed <= proven:
        reasons.append(f"unproven models in policy: {', '.join(sorted(allowed - proven))}")
    return Evidence(
        not reasons,
        reasons or ["evidence accepted"],
        {"shadow_run_id": run.id, "proven_models": sorted(proven)},
    )


class PolicyService:
    def __init__(
        self, db: Database, *, entitlements: EntitlementService, resolver_cache: TieredCache
    ) -> None:
        self._db = db
        self._ent = entitlements
        self._cache = resolver_cache

    async def _invalidate(self, s: AsyncSession, account_id: str) -> None:
        await outbox.enqueue(
            s,
            outbox.CONFIG_CHANGED,
            {"kind": "policies", "account_id": account_id},
            key=self._cache.key(account_id, "snapshot"),
        )

    async def _after_commit(self, account_id: str) -> None:
        await self._cache.invalidate(account_id, "snapshot")

    @staticmethod
    def _validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return to_dict(from_dict(spec))  # canonical form
        except PolicySpecError as exc:
            raise ValidationFailed(str(exc)) from exc

    # ---- authoring -------------------------------------------------------------------
    async def create_policy(
        self,
        *,
        account_id: str,
        name: str,
        scope: str,
        scope_ref: str,
        spec: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        await self._ent.require(account_id, "policies")
        if scope not in SCOPES:
            raise ValidationFailed("scope must be account or workload")
        if scope == "workload" and not scope_ref.strip():
            raise ValidationFailed("workload policies need scope_ref (the workload name)")
        canonical = self._validate_spec(spec)
        try:
            async with self._db.tenant(account_id) as s:
                pol = ExecutionPolicyRow(
                    id=new_id("pol"),
                    account_id=account_id,
                    name=name.strip()[:120] or scope,
                    scope=scope,
                    scope_ref=scope_ref.strip() if scope == "workload" else "",
                    created_by=actor_id,
                )
                s.add(pol)
                await s.flush()
                ver = PolicyVersion(
                    id=new_id("polv"),
                    policy_id=pol.id,
                    account_id=account_id,
                    version=1,
                    spec=canonical,
                    status="draft",
                    created_by=actor_id,
                )
                s.add(ver)
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_id,
                    account_id=account_id,
                    action="policy.create",
                    target_type="policy",
                    target_id=pol.id,
                    after={"scope": scope, "scope_ref": pol.scope_ref, "spec": canonical},
                )
                return {"policy_id": pol.id, "version_id": ver.id}
        except IntegrityError as exc:
            raise Conflict("a policy already exists for this scope") from exc

    async def add_version(
        self, *, account_id: str, policy_id: str, spec: dict[str, Any], note: str, actor_id: str
    ) -> str:
        canonical = self._validate_spec(spec)
        async with self._db.tenant(account_id) as s:
            pol = await s.get(ExecutionPolicyRow, policy_id, with_for_update=True)
            if pol is None:
                raise NotFound("policy not found")
            n = await s.scalar(
                select(func.coalesce(func.max(PolicyVersion.version), 0)).where(
                    PolicyVersion.policy_id == policy_id
                )
            )
            ver = PolicyVersion(
                id=new_id("polv"),
                policy_id=policy_id,
                account_id=account_id,
                version=int(n or 0) + 1,
                spec=canonical,
                status="draft",
                note=note[:500],
                created_by=actor_id,
            )
            s.add(ver)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="policy.version",
                target_type="policy_version",
                target_id=ver.id,
                after={"version": ver.version, "spec": canonical},
            )
            return ver.id

    async def list(self, account_id: str) -> list[dict[str, Any]]:
        async with self._db.tenant(account_id) as s:
            pols = list(
                await s.scalars(select(ExecutionPolicyRow).order_by(ExecutionPolicyRow.created_at))
            )
            versions = list(await s.scalars(select(PolicyVersion).order_by(PolicyVersion.version)))
            deps = list(await s.scalars(select(Deployment).order_by(Deployment.created_at.desc())))
        return [
            {
                "id": p.id,
                "name": p.name,
                "scope": p.scope,
                "scope_ref": p.scope_ref,
                "active_version_id": p.active_version_id,
                "versions": [
                    {
                        "id": v.id,
                        "version": v.version,
                        "status": v.status,
                        "spec": v.spec,
                        "note": v.note,
                        "evidence": v.evidence,
                    }
                    for v in versions
                    if v.policy_id == p.id
                ],
                "deployments": [
                    {
                        "id": d.id,
                        "status": d.status,
                        "percent": d.percent,
                        "to_version_id": d.to_version_id,
                        "from_version_id": d.from_version_id,
                        "rollback_reason": d.rollback_reason,
                        "history": d.history,
                    }
                    for d in deps
                    if d.policy_id == p.id
                ],
            }
            for p in pols
        ]

    # ---- deployment ------------------------------------------------------------------
    async def deploy(
        self,
        *,
        account_id: str,
        policy_id: str,
        version_id: str,
        strategy: str,
        actor_id: str,
        shadow_run_id: str | None = None,
        force_reason: str | None = None,
        percent: int | None = None,
    ) -> dict[str, Any]:
        """``immediate`` activates now; ``canary`` starts at 1% and is advanced or rolled
        back by the canary monitor; ``experiment`` holds a fixed split (spec §152) until a
        human concludes it. All require shadow evidence unless forced, and a forced
        deployment is audited with its reason."""
        if strategy not in ("immediate", "canary", "experiment"):
            raise ValidationFailed("strategy must be immediate, canary or experiment")
        if strategy == "experiment" and percent not in (5, 10, 20, 25, 50):
            raise ValidationFailed("experiments split traffic at 5, 10, 20, 25 or 50 percent")
        await self._ent.require(account_id, "policies")
        if strategy in ("canary", "experiment"):
            await self._ent.require(account_id, "canary_rollout")
        async with self._db.tenant(account_id) as s:
            pol = await s.get(ExecutionPolicyRow, policy_id, with_for_update=True)
            ver = await s.get(PolicyVersion, version_id)
            if pol is None or ver is None or ver.policy_id != policy_id:
                raise NotFound("policy version not found")
            if ver.status not in ("draft", "rolled_back"):
                raise Conflict(f"version is {ver.status}; create a new version to redeploy")
            run = await s.get(ShadowRun, shadow_run_id) if shadow_run_id else None
            evidence = check_evidence(ver.spec, run)
            if not evidence.ok and not (force_reason and force_reason.strip()):
                raise Forbidden(
                    "deployment blocked by safety gate: " + "; ".join(evidence.reasons),
                    reasons=evidence.reasons,
                )
            ver.evidence = evidence.detail | {
                "gate": evidence.reasons,
                "forced": not evidence.ok,
                "force_reason": force_reason,
            }
            now = datetime.now(UTC)
            result: dict[str, Any]
            if strategy == "immediate":
                await self._promote(s, pol, ver)
                result = {"status": "active", "version_id": ver.id}
            else:
                ver.status = "canary"
                start_pct = percent if strategy == "experiment" and percent else CANARY_STAGES[0]
                dep = Deployment(
                    id=new_id("dep"),
                    account_id=account_id,
                    policy_id=policy_id,
                    from_version_id=pol.active_version_id,
                    to_version_id=ver.id,
                    status="canary",
                    mode="experiment" if strategy == "experiment" else "canary",
                    percent=start_pct,
                    stage_started_at=now,
                    history=[{"at": now.isoformat(), "percent": start_pct, "event": "start"}],
                    created_by=actor_id,
                )
                s.add(dep)
                try:
                    await s.flush()
                except IntegrityError as exc:
                    raise Conflict("a canary is already in progress for this policy") from exc
                result = {
                    "status": "canary",
                    "mode": dep.mode,
                    "deployment_id": dep.id,
                    "percent": dep.percent,
                }
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action=f"policy.deploy.{strategy}",
                target_type="policy_version",
                target_id=ver.id,
                after={"evidence_ok": evidence.ok, "force_reason": force_reason},
            )
            await self._invalidate(s, account_id)
        await self._after_commit(account_id)
        return result

    async def _promote(self, s: AsyncSession, pol: ExecutionPolicyRow, ver: PolicyVersion) -> None:
        if pol.active_version_id and pol.active_version_id != ver.id:
            old = await s.get(PolicyVersion, pol.active_version_id)
            if old is not None:
                old.status = "superseded"
        ver.status = "active"
        pol.active_version_id = ver.id

    async def rollback(
        self, *, account_id: str, deployment_id: str, reason: str, actor: tuple[str, str]
    ) -> None:
        async with self._db.tenant(account_id) as s:
            await self._rollback(s, account_id, deployment_id, reason, actor)
        await self._after_commit(account_id)

    async def _rollback(
        self,
        s: AsyncSession,
        account_id: str,
        deployment_id: str,
        reason: str,
        actor: tuple[str, str],
    ) -> None:
        dep = await s.get(Deployment, deployment_id, with_for_update=True)
        if dep is None:
            raise NotFound("deployment not found")
        if dep.status != "canary":
            raise Conflict(f"deployment is {dep.status}")
        dep.status = "rolled_back"
        dep.rollback_reason = reason[:1000]
        dep.history = [
            *dep.history,
            {"at": datetime.now(UTC).isoformat(), "event": "rollback", "reason": reason},
        ]
        ver = await s.get(PolicyVersion, dep.to_version_id)
        if ver is not None:
            ver.status = "rolled_back"
        await audit.record(
            s,
            actor_type=actor[0],
            actor_id=actor[1],
            account_id=account_id,
            action="policy.rollback",
            target_type="deployment",
            target_id=deployment_id,
            after={"reason": reason},
        )
        await self._invalidate(s, account_id)

    async def advance(
        self, *, account_id: str, deployment_id: str, actor: tuple[str, str]
    ) -> dict[str, Any]:
        async with self._db.tenant(account_id) as s:
            dep = await s.get(Deployment, deployment_id, with_for_update=True)
            if dep is None or dep.status != "canary":
                raise Conflict("no in-flight canary")
            now = datetime.now(UTC)
            nxt = next((p for p in CANARY_STAGES if p > dep.percent), None)
            event: dict[str, Any]
            if nxt is None:  # at 100%: complete
                pol = await s.get(ExecutionPolicyRow, dep.policy_id, with_for_update=True)
                ver = await s.get(PolicyVersion, dep.to_version_id)
                assert pol is not None and ver is not None
                await self._promote(s, pol, ver)
                dep.status = "completed"
                event = {"at": now.isoformat(), "event": "completed"}
            else:
                dep.percent = nxt
                dep.stage_started_at = now
                event = {"at": now.isoformat(), "event": "advance", "percent": nxt}
            dep.history = [*dep.history, event]
            await audit.record(
                s,
                actor_type=actor[0],
                actor_id=actor[1],
                account_id=account_id,
                action="policy.canary." + event["event"],
                target_type="deployment",
                target_id=deployment_id,
                after=event,
            )
            await self._invalidate(s, account_id)
            result = {"status": dep.status, "percent": dep.percent}
        await self._after_commit(account_id)
        return result

    async def conclude_experiment(
        self, *, account_id: str, deployment_id: str, promote: bool, actor_id: str
    ) -> dict[str, Any]:
        """End an experiment: promote B to 100% (it becomes the active version) or stop it
        (back to A). Experiments never conclude automatically."""
        async with self._db.tenant(account_id) as s:
            dep = await s.get(Deployment, deployment_id, with_for_update=True)
            if dep is None or dep.status != "canary" or dep.mode != "experiment":
                raise Conflict("no running experiment with that id")
            if not promote:
                await self._rollback(
                    s, account_id, deployment_id, "experiment stopped", ("user", actor_id)
                )
                outcome = {"status": "stopped"}
            else:
                pol = await s.get(ExecutionPolicyRow, dep.policy_id, with_for_update=True)
                ver = await s.get(PolicyVersion, dep.to_version_id)
                assert pol is not None and ver is not None
                await self._promote(s, pol, ver)
                dep.status = "completed"
                dep.percent = 100
                dep.history = [
                    *dep.history,
                    {"at": datetime.now(UTC).isoformat(), "event": "promoted"},
                ]
                await audit.record(
                    s,
                    actor_type="user",
                    actor_id=actor_id,
                    account_id=account_id,
                    action="policy.experiment.promote",
                    target_type="deployment",
                    target_id=deployment_id,
                )
                await self._invalidate(s, account_id)
                outcome = {"status": "promoted"}
        await self._after_commit(account_id)
        return outcome
