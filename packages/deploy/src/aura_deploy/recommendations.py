"""Recommendation engine (plan Phase 12): turn X-Ray findings and shadow evidence into
concrete, applicable policy drafts. Nothing is deployed from here. A recommendation
becomes a *draft* policy version, which then goes through the evidence-gated deploy.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from aura_db import Database
from aura_db.models import ExecutionPolicyRow, ShadowRun, XRayReport
from aura_deploy.service import PolicyService
from aura_shared.errors import NotFound, ValidationFailed


def workload_ref(workload_key: str, label: str) -> str:
    """X-Ray keys are fingerprints. Runtime workloads are names the client sends as
    ``aura.workload``. A tagged workload keeps its tag; otherwise a slug of the label is
    suggested."""
    if workload_key.startswith("tag:"):
        return workload_key[4:]
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "workload"


def _proven(shadow_runs: list[dict[str, Any]], workload_key: str) -> dict[str, Any] | None:
    for run in shadow_runs:
        if run["status"] != "completed":
            continue
        if run["config"].get("workload_key") not in (None, workload_key):
            continue
        for c in run["results"].get("candidates", []):
            if c.get("deploy_recommended"):
                return {
                    "shadow_run_id": run["id"],
                    "model": c["model"],
                    "monthly_savings_usd": c.get("projection", {}).get("monthly_savings_usd"),
                }
    return None


def recommend(report: dict[str, Any], shadow_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure mapping. Each item has a ``type``: ``policy`` (applicable spec),
    ``run_shadow`` (evidence needed first) or ``advice`` (no policy field applies)."""
    workloads = {w["key"]: w for w in report.get("workloads", [])}
    out: list[dict[str, Any]] = []
    for opp in report.get("opportunities", []):
        wk = opp["workload_key"]
        w = workloads.get(wk, {})
        ref = workload_ref(wk, opp["workload"])
        base = {
            "kind": opp["kind"],
            "title": opp["title"],
            "workload": opp["workload"],
            "workload_key": wk,
            "suggested_workload_name": ref,
            "expected_monthly_savings_usd": opp["expected_monthly_savings_usd"],
            "confidence": opp["confidence"],
        }
        kind = opp["kind"]
        if kind == "model_downgrade":
            proof = _proven(shadow_runs, wk)
            if proof:
                out.append(
                    base
                    | {
                        "type": "policy",
                        "spec": {"allowed_models": [proof["model"]]},
                        "evidence": proof,
                        "next_step": "apply as draft, then deploy with the shadow run as evidence",
                    }
                )
            else:
                out.append(
                    base
                    | {
                        "type": "run_shadow",
                        "next_step": "run a shadow test of a small-tier model on this workload",
                    }
                )
        elif kind == "exact_cache":
            out.append(
                base
                | {
                    "type": "policy",
                    "spec": {"cache_mode": "exact"},
                    "next_step": "apply as draft; caching needs no shadow evidence",
                }
            )
        elif kind == "semantic_cache":
            out.append(
                base
                | {
                    "type": "policy",
                    "spec": {"cache_mode": "semantic", "cache_similarity": 0.95},
                    "next_step": "apply as draft and start an experiment at 10%",
                }
            )
        elif kind == "context_trimming":
            p50 = int(w.get("input_tokens_p50") or 8000)
            out.append(
                base
                | {
                    "type": "policy",
                    "spec": {
                        "context_mode": "auto",
                        "context_token_budget": max(1024, int(p50 * 0.7)),
                    },
                    "next_step": "apply as draft and start an experiment at 10%",
                }
            )
        elif kind == "prompt_caching":
            out.append(
                base
                | {
                    "type": "advice",
                    "advice": "Mark the stable system prompt prefix for provider "
                    "prompt caching (e.g. Anthropic cache_control).",
                }
            )
        elif kind == "truncation_waste":
            out.append(
                base
                | {
                    "type": "advice",
                    "advice": "Raise max_tokens for this workload or ask for "
                    "shorter output: truncated answers get retried.",
                }
            )
        elif kind == "failed_calls":
            out.append(
                base
                | {
                    "type": "advice",
                    "advice": "Send this workload through the AURA runtime for "
                    "automatic failover and circuit breaking.",
                }
            )
    return out


class RecommendationService:
    def __init__(self, db: Database, *, policies: PolicyService) -> None:
        self._db = db
        self._policies = policies

    async def list(self, account_id: str, xray_report_id: str) -> list[dict[str, Any]]:
        async with self._db.tenant(account_id) as s:
            report = await s.get(XRayReport, xray_report_id)
            if report is None:
                raise NotFound("X-Ray report not found")
            runs = [
                {"id": r.id, "status": r.status, "config": r.config, "results": r.results}
                for r in await s.scalars(select(ShadowRun).order_by(ShadowRun.created_at.desc()))
            ]
        return recommend(report.report, runs)

    async def apply(
        self, account_id: str, *, xray_report_id: str, index: int, actor_id: str
    ) -> dict[str, Any]:
        recs = await self.list(account_id, xray_report_id)
        if not 0 <= index < len(recs):
            raise NotFound("recommendation not found")
        rec = recs[index]
        if rec["type"] != "policy":
            raise ValidationFailed(
                f"this recommendation is {rec['type']}, not an applicable policy"
            )
        ref = rec["suggested_workload_name"]
        async with self._db.tenant(account_id) as s:
            existing = await s.scalar(
                select(ExecutionPolicyRow).where(
                    ExecutionPolicyRow.scope == "workload", ExecutionPolicyRow.scope_ref == ref
                )
            )
        if existing is None:
            created = await self._policies.create_policy(
                account_id=account_id,
                name=f"{rec['kind']}: {rec['workload']}"[:120],
                scope="workload",
                scope_ref=ref,
                spec=rec["spec"],
                actor_id=actor_id,
            )
            return {
                "policy_id": created["policy_id"],
                "version_id": created["version_id"],
                "workload": ref,
                "evidence": rec.get("evidence"),
            }
        version_id = await self._policies.add_version(
            account_id=account_id,
            policy_id=existing.id,
            spec=rec["spec"],
            note=f"from recommendation: {rec['title']}",
            actor_id=actor_id,
        )
        return {
            "policy_id": existing.id,
            "version_id": version_id,
            "workload": ref,
            "evidence": rec.get("evidence"),
        }
