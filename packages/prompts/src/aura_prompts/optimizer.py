"""Prompt optimizer (spec §50, plan Phase 9).

Each deterministic candidate is replayed on the customer's eval inputs. A candidate is
scored against the reference answers when they exist, otherwise against the current
prompt's own answers. It is recommended only if the evaluation gate passes *and* it
uses at least 5% fewer prompt tokens. Candidates are saved as draft versions with their
evaluation, and a human (or Autopilot, V4) activates them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

from aura_billing.application.entitlements import EntitlementService
from aura_db import Database, audit, jobs
from aura_db.jobs import ClaimedJob
from aura_db.models import PromptOptimization, PromptTemplate, PromptVersion
from aura_evaluation import JudgeVerdict, evaluate_sample, gate, summarize
from aura_prompts.registry import PromptRegistry
from aura_prompts.templates import candidates, render, variables
from aura_providers.domain.catalog import ModelCatalog, ModelSpec
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.types import ChatRequest, Message, ProviderError
from aura_providers.registry import ProviderRegistry
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.ids import new_id

log = logging.getLogger("aura.prompts.optimizer")
JOB_KIND = "prompt_optimize"
MAX_ITEMS = 100
MIN_TOKEN_SAVINGS_PCT = 5.0


def _aad(account_id: str, run_id: str) -> str:
    return f"{account_id}|prompt_opt|{run_id}"


class PromptOptimizerService:
    def __init__(
        self,
        db: Database,
        *,
        envelope: Envelope,
        catalog: ModelCatalog,
        entitlements: EntitlementService,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._catalog = catalog
        self._ent = entitlements

    async def start(
        self,
        account_id: str,
        *,
        template_id: str,
        model: str,
        items: list[dict[str, Any]],
        actor_id: str,
        budget_usd: Decimal = Decimal("2"),
    ) -> dict[str, str]:
        await self._ent.require(account_id, "prompt_optimization")
        spec = self._catalog.resolve(model)
        if spec is None:
            raise ValidationFailed(f"unknown model {model!r}")
        if not 1 <= len(items) <= MAX_ITEMS:
            raise ValidationFailed(f"provide 1-{MAX_ITEMS} eval items")
        async with self._db.tenant(account_id) as s:
            tpl = await s.get(PromptTemplate, template_id)
            if tpl is None or tpl.active_version_id is None:
                raise NotFound("prompt not found")
            current = await s.get(PromptVersion, tpl.active_version_id)
            assert current is not None
            needed = variables(current.content)
            for i, item in enumerate(items):
                if not str(item.get("input", "")).strip():
                    raise ValidationFailed(f"item {i}: input is required")
                if missing := needed - set(item.get("vars") or {}):
                    raise ValidationFailed(f"item {i}: missing variables {sorted(missing)}")
            run_id = new_id("popt")
            sealed = self._envelope.seal(json.dumps(items), aad=_aad(account_id, run_id))
            s.add(
                PromptOptimization(
                    id=run_id,
                    account_id=account_id,
                    template_id=template_id,
                    model=spec.id,
                    status="queued",
                    results={"budget_usd": str(budget_usd)},
                    dataset_key_id=sealed.key_id,
                    dataset_wrapped_dek=sealed.wrapped_dek,
                    dataset_nonce=sealed.nonce,
                    dataset_ciphertext=sealed.ciphertext,
                    created_by=actor_id,
                )
            )
            await jobs.enqueue(s, JOB_KIND, {"run_id": run_id}, account_id=account_id)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_id,
                account_id=account_id,
                action="prompt.optimize",
                target_type="prompt_optimization",
                target_id=run_id,
                after={"items": len(items), "model": spec.id},
            )
        return {"run_id": run_id, "status": "queued"}

    async def get(self, account_id: str, run_id: str) -> dict[str, Any]:
        async with self._db.tenant(account_id) as s:
            row = await s.get(PromptOptimization, run_id)
            if row is None:
                raise NotFound("optimization run not found")
            return {
                "id": row.id,
                "status": row.status,
                "model": row.model,
                "results": row.results,
                "spent_usd": str(row.spent_usd),
                "error": row.error,
            }


class PromptOptimizationRunner:
    def __init__(
        self,
        db: Database,
        *,
        envelope: Envelope,
        catalog: ModelCatalog,
        registry: ProviderRegistry,
        prompts: PromptRegistry,
        concurrency: int = 4,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._catalog = catalog
        self._registry = registry
        self._prompts = prompts
        self._sem = asyncio.Semaphore(concurrency)

    async def _call(
        self, account_id: str, spec: ModelSpec, system: str, user: str
    ) -> tuple[str, int, Decimal]:
        async with self._sem:
            provider = self._registry.provider_for(spec)
            creds = await self._registry.credentials_for(account_id, spec)
            r = await provider.complete(
                spec,
                ChatRequest((Message("system", system), Message("user", user))),
                creds,
                max_tokens=min(1024, spec.max_output),
            )
        return r.text, r.usage.input_tokens, cost_usd(spec, r.usage)

    async def process(self, job: ClaimedJob) -> None:
        run_id, account_id = job.payload["run_id"], job.account_id
        assert account_id is not None
        async with self._db.tenant(account_id) as s:
            row = await s.get(PromptOptimization, run_id)
            if row is None or row.status in ("completed", "failed"):
                return
            row.status = "running"
            tpl = await s.get(PromptTemplate, row.template_id)
            assert tpl is not None and tpl.active_version_id is not None
            current = await s.get(PromptVersion, tpl.active_version_id)
            assert current is not None
            sealed = Sealed(
                row.dataset_key_id or "",
                row.dataset_wrapped_dek or b"",
                row.dataset_nonce or b"",
                row.dataset_ciphertext or b"",
            )
            model_id, budget = row.model, Decimal(row.results.get("budget_usd", "2"))
            template_id, baseline_text = tpl.id, current.content
        try:
            items = json.loads(self._envelope.open(sealed, aad=_aad(account_id, run_id)))
            results, spent, _version_ids = await self._optimize(
                account_id, template_id, baseline_text, model_id, items, budget
            )
            status, error = "completed", None
        except Exception as exc:
            if job.attempts < job.max_attempts:
                await self._finish(
                    account_id, run_id, "queued", {}, Decimal(0), str(exc), erase=False
                )
                raise
            results, spent, status, error = {}, Decimal(0), "failed", str(exc)[:2000]
        await self._finish(account_id, run_id, status, results, spent, error, erase=True)

    async def _finish(
        self,
        account_id: str,
        run_id: str,
        status: str,
        results: dict[str, Any],
        spent: Decimal,
        error: str | None,
        *,
        erase: bool,
    ) -> None:
        async with self._db.tenant(account_id) as s:
            row = await s.get(PromptOptimization, run_id)
            assert row is not None
            row.status, row.results, row.spent_usd, row.error = status, results, float(spent), error
            if erase:
                row.dataset_key_id = row.dataset_wrapped_dek = None
                row.dataset_nonce = row.dataset_ciphertext = None

    async def _optimize(
        self,
        account_id: str,
        template_id: str,
        baseline_tpl: str,
        model_id: str,
        items: list[dict[str, Any]],
        budget: Decimal,
    ) -> tuple[dict[str, Any], Decimal, list[str]]:
        spec = self._catalog.get(model_id)
        if spec is None:
            raise ValidationFailed("model no longer available")
        spent = Decimal(0)

        async def run(tpl: str) -> list[tuple[str, int, Decimal] | None]:
            nonlocal spent
            out: list[tuple[str, int, Decimal] | None] = []
            for item in items:
                if spent > budget:
                    out.append(None)
                    continue
                try:
                    res = await self._call(
                        account_id, spec, render(tpl, item.get("vars") or {}), item["input"]
                    )
                except ProviderError:
                    out.append(None)
                    continue
                spent += res[2]
                out.append(res)
            return out

        baseline = await run(baseline_tpl)
        report = []
        version_ids = []
        base_tokens = sum(r[1] for r in baseline if r) or 1
        for name, cand_tpl in candidates(baseline_tpl):
            outs = await run(cand_tpl)
            evals = []
            cand_tokens = 0
            for item, b, c in zip(items, baseline, outs, strict=True):
                if b is None or c is None:
                    continue
                yardstick = item.get("reference") or b[0]
                evals.append(
                    evaluate_sample(
                        baseline=yardstick,
                        candidate=c[0],
                        verdict=JudgeVerdict.UNKNOWN,
                        baseline_cost=b[2],
                        candidate_cost=c[2],
                        baseline_latency_ms=None,
                        candidate_latency_ms=None,
                    )
                )
                cand_tokens += c[1]
            if not evals:
                continue
            summary = summarize(f"candidate:{name}", evals)
            decision = gate(summary, min_samples=min(30, len(items)), min_savings_pct=0.0)
            token_savings = (1 - cand_tokens / base_tokens) * 100
            recommended = decision.deploy and token_savings >= MIN_TOKEN_SAVINGS_PCT
            evaluation = summary.to_dict() | {
                "transform": name,
                "prompt_token_savings_pct": round(token_savings, 2),
                "recommended": recommended,
                "gate_reasons": decision.reasons
                + (
                    []
                    if token_savings >= MIN_TOKEN_SAVINGS_PCT
                    else ["prompt token savings below 5%"]
                ),
            }
            vid = await self._prompts.add_version(
                account_id,
                template_id=template_id,
                content=cand_tpl,
                actor_id=None,
                origin=f"optimizer:{name}",
                evaluation=evaluation,
            )
            version_ids.append(vid)
            report.append(evaluation | {"version_id": vid})
        report.sort(key=lambda r: (not r["recommended"], -r["prompt_token_savings_pct"]))
        return (
            {
                "candidates": report,
                "items": len(items),
                "budget_exhausted": spent > budget,
                "baseline_prompt_tokens": base_tokens,
            },
            spent,
            version_ids,
        )
