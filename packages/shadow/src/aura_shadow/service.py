"""Shadow runs (spec §26): production keeps running unchanged while AURA replays a sample
of real traffic through candidate policies and reports CURRENT vs AURA.

Data handling:
- Replay inputs (prompts) are envelope-encrypted at rest (AAD = account|shadow|run).
- The dataset is erased as soon as the run finishes, whether it succeeds or fails for
  good.
- Results hold only aggregates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from aura_billing.application.entitlements import EntitlementService
from aura_db import Database, audit, jobs
from aura_db.jobs import ClaimedJob
from aura_db.models import ShadowRun
from aura_evaluation import (
    JudgeVerdict,
    SampleEvaluation,
    compare,
    evaluate_sample,
    gate,
    summarize,
)
from aura_providers.domain.catalog import ModelCatalog, ModelSpec
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.types import ChatRequest, Message, ProviderError, Usage
from aura_providers.registry import ProviderRegistry
from aura_shared.crypto import Envelope, Sealed
from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.ids import new_id
from aura_xray.domain.parsers import parse
from aura_xray.domain.report import DAYS_PER_MONTH, _window
from aura_xray.domain.workloads import discover

log = logging.getLogger("aura.shadow")
JOB_KIND = "shadow_run"
MAX_SAMPLES = 1_000
MAX_CANDIDATES = 5


@dataclass(frozen=True, slots=True)
class ShadowView:
    id: str
    name: str
    status: str
    config: dict[str, Any]
    results: dict[str, Any]
    spent_usd: Decimal
    error: str | None
    created_at: datetime | None


def _view(r: ShadowRun) -> ShadowView:
    return ShadowView(
        r.id,
        r.name,
        r.status,
        r.config,
        r.results,
        Decimal(str(r.spent_usd)),
        r.error,
        r.created_at,
    )


def _aad(account_id: str, run_id: str) -> str:
    return f"{account_id}|shadow|{run_id}"


class ShadowService:
    """Control-plane side: validate, sample, seal, enqueue."""

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

    async def create(
        self,
        *,
        account_id: str,
        data: bytes,
        fmt: str,
        candidates: list[str],
        actor_user_id: str,
        workload_key: str | None = None,
        sample_size: int = 100,
        budget_usd: Decimal = Decimal("5"),
        judge_model: str | None = None,
        quality_target: float = 0.9,
        name: str = "",
        seed: int = 7,
    ) -> ShadowView:
        await self._ent.require(account_id, "shadow_optimization")
        if not 1 <= len(candidates) <= MAX_CANDIDATES:
            raise ValidationFailed(f"provide 1-{MAX_CANDIDATES} candidate models")
        specs = []
        for c in candidates:
            spec = self._catalog.resolve(c)
            if spec is None:
                raise ValidationFailed(f"unknown candidate model {c!r}")
            specs.append(spec)
        judge = self._catalog.resolve(judge_model) if judge_model else None
        if judge_model and judge is None:
            raise ValidationFailed(f"unknown judge model {judge_model!r}")
        if not 1 <= sample_size <= MAX_SAMPLES:
            raise ValidationFailed(f"sample_size must be 1-{MAX_SAMPLES}")
        if budget_usd <= 0:
            raise ValidationFailed("budget_usd must be positive")

        parsed = parse(data, fmt)
        records = [r for r in parsed.records if r.user_content and r.status == "ok"]
        workloads = discover(records)
        if workload_key:
            match = next((w for w in workloads if w.key == workload_key), None)
            if match is None:
                raise ValidationFailed("workload not found in the uploaded traffic")
            pool = [records[i] for i in match.record_indexes]
            label = match.label
        else:
            pool, label = records, "all traffic"
        if not pool:
            raise ValidationFailed("no replayable records (requests need prompt content)")

        _, _, days = _window(pool)
        monthly_requests = int(Decimal(len(pool)) * DAYS_PER_MONTH / days)
        rng = random.Random(seed)  # noqa: S311 - sampling, not crypto
        sample = rng.sample(pool, min(sample_size, len(pool)))
        dataset = [
            {
                "system": r.system_prompt,
                "user": r.user_content,
                "model": r.model,
                "response": r.response_text,
                "in": r.input_tokens,
                "out": r.output_tokens,
            }
            for r in sample
        ]
        replays = len(sample) * len(specs)
        reservation = await self._ent.reserve(
            account_id, "shadow_runs", amount=replays, reservation_id=new_id("shd")
        )
        await self._ent.commit(reservation, actual=replays)

        run_id = new_id("shd")
        sealed = self._envelope.seal(json.dumps(dataset), aad=_aad(account_id, run_id))
        config = {
            "workload_key": workload_key,
            "workload": label,
            "candidates": [s.id for s in specs],
            "judge_model": judge.id if judge else None,
            "sample_size": len(sample),
            "budget_usd": str(budget_usd),
            "quality_target": quality_target,
            "monthly_requests": monthly_requests,
            "seed": seed,
        }
        async with self._db.tenant(account_id) as s:
            row = ShadowRun(
                id=run_id,
                account_id=account_id,
                name=name.strip()[:200] or f"Shadow: {label}",
                status="queued",
                config=config,
                dataset_key_id=sealed.key_id,
                dataset_wrapped_dek=sealed.wrapped_dek,
                dataset_nonce=sealed.nonce,
                dataset_ciphertext=sealed.ciphertext,
                created_by=actor_user_id,
            )
            s.add(row)
            await jobs.enqueue(s, JOB_KIND, {"run_id": run_id}, account_id=account_id)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=account_id,
                action="shadow.create",
                target_type="shadow_run",
                target_id=run_id,
                after={k: config[k] for k in ("candidates", "sample_size", "budget_usd")},
            )
            await s.flush()
            await s.refresh(row)
            return _view(row)

    async def list(self, account_id: str) -> list[ShadowView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(ShadowRun).order_by(ShadowRun.created_at.desc()))
            return [_view(r) for r in rows]

    async def get(self, account_id: str, run_id: str) -> ShadowView:
        async with self._db.tenant(account_id) as s:
            row = await s.get(ShadowRun, run_id)
            if row is None:
                raise NotFound("shadow run not found")
            return _view(row)


class _BudgetExhausted(Exception):
    pass


class ShadowRunner:
    """Worker side: executes a queued shadow run."""

    def __init__(
        self,
        db: Database,
        *,
        envelope: Envelope,
        catalog: ModelCatalog,
        registry: ProviderRegistry,
        concurrency: int = 4,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._catalog = catalog
        self._registry = registry
        self._sem = asyncio.Semaphore(concurrency)

    async def process(self, job: ClaimedJob) -> None:
        run_id, account_id = job.payload["run_id"], job.account_id
        assert account_id is not None
        async with self._db.tenant(account_id) as s:
            row = await s.get(ShadowRun, run_id)
            if row is None or row.status in ("completed", "failed"):
                return  # idempotent: duplicate or late job delivery
            if row.dataset_ciphertext is None:
                raise RuntimeError("dataset already erased")
            row.status = "running"
            sealed = Sealed(
                row.dataset_key_id or "",
                row.dataset_wrapped_dek or b"",
                row.dataset_nonce or b"",
                row.dataset_ciphertext,
            )
            config = dict(row.config)
        try:
            dataset: list[dict[str, Any]] = json.loads(
                self._envelope.open(sealed, aad=_aad(account_id, run_id))
            )
            results, spent = await self._execute(account_id, dataset, config)
        except Exception as exc:
            final = job.attempts >= job.max_attempts
            await self._finish(
                account_id,
                run_id,
                status="failed" if final else "queued",
                results={},
                spent=Decimal(0),
                error=str(exc)[:2000],
                erase=final,
            )
            raise
        await self._finish(
            account_id,
            run_id,
            status="completed",
            results=results,
            spent=spent,
            error=None,
            erase=True,
        )

    async def _finish(
        self,
        account_id: str,
        run_id: str,
        *,
        status: str,
        results: dict[str, Any],
        spent: Decimal,
        error: str | None,
        erase: bool,
    ) -> None:
        async with self._db.tenant(account_id) as s:
            row = await s.get(ShadowRun, run_id)
            assert row is not None
            row.status = status
            row.results = results
            row.spent_usd = float(spent)
            row.error = error
            if erase:
                row.dataset_key_id = row.dataset_wrapped_dek = None
                row.dataset_nonce = row.dataset_ciphertext = None

    async def _call(
        self, account_id: str, spec: ModelSpec, req: ChatRequest, max_tokens: int
    ) -> tuple[str, Decimal, int]:
        async with self._sem:
            provider = self._registry.provider_for(spec)
            creds = await self._registry.credentials_for(account_id, spec)
            t0 = time.monotonic()
            result = await provider.complete(spec, req, creds, max_tokens=max_tokens)
            latency = int((time.monotonic() - t0) * 1000)
        served = next(
            (
                m
                for m in self._catalog.models.values()
                if m.provider == spec.provider and m.upstream == result.model_served
            ),
            spec,
        )
        return result.text, cost_usd(served, result.usage), latency

    async def _execute(
        self, account_id: str, dataset: list[dict[str, Any]], config: dict[str, Any]
    ) -> tuple[dict[str, Any], Decimal]:
        budget = Decimal(config["budget_usd"])
        spent = Decimal(0)
        lock = asyncio.Lock()
        candidates = [self._catalog.get(c) for c in config["candidates"]]
        judge_spec = self._catalog.get(config["judge_model"]) if config.get("judge_model") else None
        evals: dict[str, list[SampleEvaluation]] = {c.id: [] for c in candidates if c}
        errors: dict[str, int] = {c.id: 0 for c in candidates if c}
        skipped = 0

        async def charge(amount: Decimal) -> None:
            nonlocal spent
            async with lock:
                spent += amount
                if spent > budget:
                    raise _BudgetExhausted

        async def one(item: dict[str, Any]) -> None:
            nonlocal skipped
            msgs = ((Message("system", item["system"]),) if item.get("system") else ()) + (
                Message("user", item["user"]),
            )
            req = ChatRequest(msgs)
            out_tokens = max(int(item.get("out") or 0) * 2, 256)
            base_spec = self._catalog.resolve(item["model"])
            if item.get("response"):
                baseline = item["response"]
                base_cost = (
                    cost_usd(base_spec, Usage(item["in"], item["out"])) if base_spec else Decimal(0)
                )
                base_latency = None
            elif base_spec is not None:
                try:
                    baseline, base_cost, base_latency = await self._call(
                        account_id, base_spec, req, min(out_tokens, base_spec.max_output)
                    )
                except ProviderError:
                    skipped += 1
                    return
                await charge(base_cost)
            else:
                skipped += 1  # no stored answer and the baseline model is not callable
                return
            for spec in candidates:
                if spec is None:
                    continue
                try:
                    text, cost, latency = await self._call(
                        account_id, spec, req, min(out_tokens, spec.max_output)
                    )
                except ProviderError:
                    errors[spec.id] += 1
                    continue
                await charge(cost)
                verdict = JudgeVerdict.UNKNOWN
                if judge_spec is not None:
                    verdict = await self._judge(
                        account_id, judge_spec, item["user"], baseline, text, charge
                    )
                evals[spec.id].append(
                    evaluate_sample(
                        baseline=baseline,
                        candidate=text,
                        verdict=verdict,
                        baseline_cost=base_cost,
                        candidate_cost=cost,
                        baseline_latency_ms=base_latency,
                        candidate_latency_ms=latency,
                    )
                )

        # TaskGroup cancels in-flight siblings the moment the budget is exhausted;
        # asyncio.gather would let them keep calling (and paying for) models.
        truncated = False
        try:
            async with asyncio.TaskGroup() as tg:
                for item in dataset:
                    tg.create_task(one(item))
        except* _BudgetExhausted:
            truncated = True

        monthly_requests = int(config["monthly_requests"])
        per_candidate = []
        for spec_id, samples in evals.items():
            if not samples:
                per_candidate.append({"model": spec_id, "samples": 0, "errors": errors[spec_id]})
                continue
            summary = summarize(spec_id, samples)
            decision = gate(summary, quality_target=float(config["quality_target"]))
            n = Decimal(len(samples))
            monthly_base = summary.baseline_cost_usd / n * monthly_requests
            monthly_cand = summary.candidate_cost_usd / n * monthly_requests
            per_candidate.append(
                summary.to_dict()
                | {
                    "errors": errors[spec_id],
                    "deploy_recommended": decision.deploy,
                    "gate_reasons": decision.reasons,
                    "projection": {
                        "monthly_requests": monthly_requests,
                        "monthly_baseline_usd": str(monthly_base.quantize(Decimal("0.01"))),
                        "monthly_candidate_usd": str(monthly_cand.quantize(Decimal("0.01"))),
                        "monthly_savings_usd": str(
                            (monthly_base - monthly_cand).quantize(Decimal("0.01"))
                        ),
                    },
                }
            )
        per_candidate.sort(
            key=lambda c: (not c.get("deploy_recommended", False), c.get("cost_change_pct", 0))
        )
        return (
            {
                "candidates": per_candidate,
                "samples_planned": len(dataset),
                "samples_skipped": skipped,
                "budget_exhausted": truncated,
                "judge_model": config.get("judge_model"),
                "method_note": (
                    "Quality = share of samples where the candidate was judged not worse than "
                    "the current production answer (position-swapped LLM judge; token-overlap "
                    "proxy where no judge verdict). Intervals are 95% Wilson / bootstrap."
                ),
            },
            spent,
        )

    async def _judge(
        self,
        account_id: str,
        judge: ModelSpec,
        request: str,
        baseline: str,
        candidate: str,
        charge: Any,
    ) -> JudgeVerdict:
        async def complete(prompt: str) -> str:
            text, cost, _ = await self._call(
                account_id, judge, ChatRequest((Message("user", prompt),)), 16
            )
            await charge(cost)
            return text

        try:
            return await compare(complete, request, baseline, candidate)
        except ProviderError:
            return JudgeVerdict.UNKNOWN
