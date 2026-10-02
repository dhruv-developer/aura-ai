"""Generic job loop: claim → dispatch by kind → complete / retry with backoff."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aura_db.jobs import ClaimedJob, JobQueue

log = logging.getLogger("aura.workers.jobs")
Handler = Callable[[ClaimedJob], Awaitable[None]]


class JobRunner:
    def __init__(self, queue: JobQueue, handlers: dict[str, Handler]) -> None:
        self._queue = queue
        self._handlers = handlers

    async def run_once(self) -> bool:
        """Process one job if available. Returns False when the queue is empty."""
        job = await self._queue.claim(list(self._handlers))
        if job is None:
            return False
        try:
            await self._handlers[job.kind](job)
        except Exception as exc:
            retry = await self._queue.fail(job, f"{type(exc).__name__}: {exc}")
            log.warning("job %s (%s) failed; retry=%s", job.id, job.kind, retry, exc_info=True)
            return True
        await self._queue.complete(job.id)
        return True

    async def run_forever(self, *, idle_sleep_s: float = 1.0) -> None:
        while True:
            try:
                busy = await self.run_once()
            except Exception:
                log.exception("job loop error")
                busy = False
            if not busy:
                await asyncio.sleep(idle_sleep_s)
