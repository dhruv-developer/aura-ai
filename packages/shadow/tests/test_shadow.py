"""Shadow runs end to end: control plane creates → worker replays → results."""

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from aura_db.jobs import JobQueue
from aura_db.models import Job, ShadowRun
from aura_shadow import ShadowRunner
from aura_shadow.service import JOB_KIND
from aura_shared.errors import Forbidden, ValidationFailed
from aura_workers.jobs_runner import JobRunner


def _dataset(n: int = 40) -> bytes:
    rows = []
    for i in range(n):
        q = f"question number {i} about refunds"
        # Half the stored production answers match what the cheap candidate returns.
        answer = f"[small] {q}" if i % 2 == 0 else "a completely different long answer text"
        rows.append(
            {
                "timestamp": f"2026-09-{1 + i % 28:02d}T00:00:00Z",
                "request": {
                    "model": "stub/frontier",
                    "messages": [
                        {"role": "system", "content": "You are refund support."},
                        {"role": "user", "content": q},
                    ],
                },
                "response": {
                    "model": "stub/frontier",
                    "usage": {"prompt_tokens": 2000, "completion_tokens": 300},
                    "choices": [{"message": {"content": answer}}],
                },
            }
        )
    return "\n".join(json.dumps(r) for r in rows).encode()


async def _org(container):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("ml@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    return user, org


def _runner(container, runtime) -> JobRunner:  # type: ignore[no-untyped-def]
    shadow_runner = ShadowRunner(
        container.db,
        envelope=container.shadow._envelope,
        catalog=runtime.engine.catalog,
        registry=runtime.engine._registry,
    )
    return JobRunner(JobQueue(container.db), {JOB_KIND: shadow_runner.process})


async def test_shadow_run_lifecycle(container, runtime) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    view = await container.shadow.create(
        account_id=org.account_id,
        data=_dataset(),
        fmt="jsonl",
        candidates=["stub/small"],
        actor_user_id=user.id,
        sample_size=40,
        budget_usd=Decimal("10"),
    )
    assert view.status == "queued" and view.config["sample_size"] == 40
    assert await container.entitlements.usage(org.account_id, "shadow_runs") == (40, 20_000)

    assert await _runner(container, runtime).run_once() is True
    done = await container.shadow.get(org.account_id, view.id)
    assert done.status == "completed", done.error
    [cand] = done.results["candidates"]
    assert cand["model"] == "stub/small" and cand["samples"] == 40
    assert cand["non_inferiority_rate"] == 0.5  # similarity proxy: half match the baseline
    assert cand["deploy_recommended"] is False  # quality lower bound far below 0.9
    assert cand["cost_change_pct"] < 0  # cheaper
    assert Decimal(cand["projection"]["monthly_savings_usd"]) > 0
    assert done.spent_usd > 0

    async with container.db.system() as s:
        row = await s.get(ShadowRun, view.id)
        job = await s.scalar(select(Job))
    assert row.dataset_ciphertext is None  # replay inputs erased after completion
    assert job.status == "done"
    assert await _runner(container, runtime).run_once() is False  # queue drained


async def test_budget_stops_replay_early(container, runtime) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    view = await container.shadow.create(
        account_id=org.account_id,
        data=_dataset(),
        fmt="jsonl",
        candidates=["stub/small"],
        actor_user_id=user.id,
        sample_size=40,
        budget_usd=Decimal("0.00002"),
    )
    await _runner(container, runtime).run_once()
    done = await container.shadow.get(org.account_id, view.id)
    assert done.status == "completed" and done.results["budget_exhausted"] is True
    assert done.results["candidates"][0]["samples"] < 40


async def test_failing_run_retries_then_fails_and_erases(container, runtime) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    view = await container.shadow.create(
        account_id=org.account_id,
        data=_dataset(4),
        fmt="jsonl",
        candidates=["stub/small"],
        actor_user_id=user.id,
        sample_size=4,
    )
    async with container.db.system() as s:  # corrupt the sealed dataset
        row = await s.get(ShadowRun, view.id)
        row.dataset_ciphertext = b"garbage"
        job = await s.scalar(select(Job))
        job.max_attempts = 1
    await _runner(container, runtime).run_once()
    failed = await container.shadow.get(org.account_id, view.id)
    async with container.db.system() as s:
        row = await s.get(ShadowRun, view.id)
        job = await s.scalar(select(Job))
    assert failed.status == "failed" and "decrypt" in (failed.error or "").lower()
    assert row.dataset_ciphertext is None and job.status == "dead"


async def test_validation_and_entitlements(container) -> None:  # type: ignore[no-untyped-def]
    user, org = await _org(container)
    with pytest.raises(ValidationFailed):
        await container.shadow.create(
            account_id=org.account_id,
            data=_dataset(),
            fmt="jsonl",
            candidates=["gpt-imaginary"],
            actor_user_id=user.id,
        )
    with pytest.raises(ValidationFailed, match="workload"):
        await container.shadow.create(
            account_id=org.account_id,
            data=_dataset(),
            fmt="jsonl",
            candidates=["stub/small"],
            actor_user_id=user.id,
            workload_key="tpl:nope",
        )
    [personal] = [
        a for a in await container.identity.list_access(user.id) if a.kind == "individual"
    ]
    with pytest.raises(Forbidden):
        await container.shadow.create(
            account_id=personal.account_id,
            data=_dataset(),
            fmt="jsonl",
            candidates=["stub/small"],
            actor_user_id=user.id,
        )
