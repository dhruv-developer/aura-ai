"""Runtime policy resolution: account policy, then workload policy, then the request.
Canary-aware and cached."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from aura_cache import TieredCache
from aura_db import Database
from aura_db.models import Deployment, ExecutionPolicyRow, PolicyVersion
from aura_deploy.assignment import variant as assign
from aura_policy import ExecutionPolicy, from_dict, merge


@dataclass(frozen=True, slots=True)
class Resolution:
    policy: ExecutionPolicy | None
    policy_id: str | None = None  # most specific policy applied
    policy_version_id: str | None = None
    deployment_id: str | None = None
    variant: str | None = None  # canary | stable (only during a deployment)


class PolicyResolver:
    def __init__(self, db: Database, *, cache: TieredCache) -> None:
        self._db = db
        self._cache = cache

    async def _snapshot(self, account_id: str) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            async with self._db.tenant(account_id) as s:
                pols = list(await s.scalars(select(ExecutionPolicyRow)))
                versions = {v.id: v for v in await s.scalars(select(PolicyVersion))}
                deps = {
                    d.policy_id: d
                    for d in await s.scalars(
                        select(Deployment).where(Deployment.status == "canary")
                    )
                }
            out: dict[str, Any] = {}
            for p in pols:
                active = versions.get(p.active_version_id or "")
                dep = deps.get(p.id)
                entry: dict[str, Any] = {
                    "policy_id": p.id,
                    "active": {"id": active.id, "spec": active.spec} if active else None,
                    "canary": None,
                }
                if dep is not None and (to := versions.get(dep.to_version_id)) is not None:
                    entry["canary"] = {
                        "deployment_id": dep.id,
                        "percent": dep.percent,
                        "id": to.id,
                        "spec": to.spec,
                    }
                key = "account" if p.scope == "account" else f"workload:{p.scope_ref}"
                out[key] = entry
            return out

        snapshot: dict[str, Any] = await self._cache.get_or_load(
            account_id, "snapshot", loader=load
        )
        return snapshot

    async def resolve(
        self, account_id: str, *, workload: str | None, sticky_key: str
    ) -> Resolution:
        snap = await self._snapshot(account_id)
        layers: list[ExecutionPolicy] = []
        applied: Resolution = Resolution(None)
        for key in ("account", f"workload:{workload}" if workload else None):
            entry = snap.get(key) if key else None
            if entry is None:
                continue
            version, dep_id, var = entry["active"], None, None
            if entry["canary"] is not None:
                c = entry["canary"]
                dep_id = c["deployment_id"]
                var = assign(dep_id, sticky_key, int(c["percent"]))
                if var == "canary":
                    version = {"id": c["id"], "spec": c["spec"]}
            if version is None:
                if dep_id:  # first-ever deployment: stable means "no policy at this scope"
                    applied = Resolution(None, entry["policy_id"], None, dep_id, var)
                continue
            layers.append(from_dict(version["spec"]))
            applied = Resolution(None, entry["policy_id"], version["id"], dep_id, var)
        if not layers and applied.deployment_id is None:
            return Resolution(None)
        return Resolution(
            merge(*layers) if layers else None,
            applied.policy_id,
            applied.policy_version_id,
            applied.deployment_id,
            applied.variant,
        )
