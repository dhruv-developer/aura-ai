"""Postgres job queue: enqueue in any transaction, claim with SKIP LOCKED, retry with
exponential backoff, visibility timeout for crashed workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aura_db.models import Job
from aura_db.session import Database
from aura_shared.ids import new_id


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    kind: str
    account_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    account_id: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
) -> str:
    """Enqueue inside the caller's transaction: the job exists iff the change commits."""
    job_id = new_id("job")
    session.add(
        Job(
            id=job_id,
            kind=kind,
            account_id=account_id,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
            run_after=datetime.now(UTC),
        )
    )
    return job_id


class JobQueue:
    def __init__(self, db: Database, *, visibility_s: int = 600) -> None:
        self._db = db
        self._visibility = timedelta(seconds=visibility_s)

    async def claim(self, kinds: list[str]) -> ClaimedJob | None:
        """Claim the next ready job. A ``running`` job whose lock expired (the worker
        died) is claimable again."""
        now = datetime.now(UTC)
        async with self._db.system() as s:
            job = await s.scalar(
                select(Job)
                .where(
                    Job.kind.in_(kinds),
                    Job.run_after <= now,
                    or_(
                        Job.status == "queued",
                        (Job.status == "running") & (Job.locked_until < now),
                    ),
                )
                .order_by(Job.priority, Job.run_after)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.locked_until = now + self._visibility
            return ClaimedJob(
                job.id, job.kind, job.account_id, dict(job.payload), job.attempts, job.max_attempts
            )

    async def complete(self, job_id: str) -> None:
        async with self._db.system() as s:
            await s.execute(
                update(Job).where(Job.id == job_id).values(status="done", locked_until=None)
            )

    async def fail(self, job: ClaimedJob, error: str) -> bool:
        """Returns True if the job will be retried."""
        retry = job.attempts < job.max_attempts
        backoff = timedelta(seconds=min(30 * 2 ** (job.attempts - 1), 3600))
        async with self._db.system() as s:
            await s.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    status="queued" if retry else "dead",
                    locked_until=None,
                    run_after=datetime.now(UTC) + backoff,
                    last_error=error[:2000],
                )
            )
        return retry
