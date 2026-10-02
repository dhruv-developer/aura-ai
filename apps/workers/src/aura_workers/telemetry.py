"""Telemetry consumer: dedupe → ClickHouse batch → schedule usage reconciliation."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from aura_shared.redis import KeyFamily, build_key, ttl_for
from aura_workers.rows import execution_rows
from aura_workers.sinks import TelemetrySink

log = logging.getLogger("aura.workers.telemetry")
Reconcile = Callable[[str], Awaitable[Any]]


class TelemetryProcessor:
    """Processes a batch of event envelopes. Transport-agnostic (NATS runner, tests).

    - Dedupe: an event id is claimed in Redis only *after* the sink write succeeds, so
      a crash between write and ack causes a redelivery, which the ReplacingMergeTree
      absorbs. A claim before writing could lose events.
    - Reconciliation: accounts seen in a batch get their Redis usage counters
      snapshotted to Postgres, at most once per ``reconcile_interval_s`` per account.
    """

    def __init__(
        self,
        *,
        sink: TelemetrySink,
        state: aioredis.Redis,
        reconcile: Reconcile | None = None,
        reconcile_interval_s: float = 60.0,
    ) -> None:
        self._sink = sink
        self._r = state
        self._reconcile = reconcile
        self._interval = reconcile_interval_s
        self._last_reconciled: dict[str, float] = {}
        self.processed = 0
        self.duplicates = 0
        self.skipped = 0

    def _seen_key(self, event_id: str) -> str:
        return build_key(KeyFamily.IDEMPOTENCY, "_global", "telemetry", event_id)

    async def process(self, envelopes: list[dict[str, Any]]) -> None:
        fresh: list[dict[str, Any]] = []
        if envelopes:
            ids = [e.get("data", {}).get("event_id", "") for e in envelopes]
            seen = await self._r.mget([self._seen_key(i) for i in ids if i])
            seen_ids = {i for i, v in zip([i for i in ids if i], seen, strict=True) if v}
            batch_ids: set[str] = set()
            for env, eid in zip(envelopes, ids, strict=True):
                if env.get("type") != "execution.completed" or not eid:
                    self.skipped += 1
                    continue
                if env.get("schema_version") != 1:
                    log.warning("unknown execution.completed schema %s", env.get("schema_version"))
                    self.skipped += 1
                    continue
                if eid in seen_ids or eid in batch_ids:
                    self.duplicates += 1
                    continue
                batch_ids.add(eid)
                fresh.append(env)
        executions, calls = [], []
        for env in fresh:
            row, call_rows = execution_rows(env)
            executions.append(row)
            calls.extend(call_rows)
        if executions:
            await self._sink.write(executions, calls)
            ttl = ttl_for(KeyFamily.IDEMPOTENCY, jitter=False) or 86_400
            async with self._r.pipeline(transaction=False) as p:
                for row in executions:
                    p.set(self._seen_key(row["event_id"]), b"1", ex=ttl)
                await p.execute()
            self.processed += len(executions)
        await self._maybe_reconcile({r["account_id"] for r in executions})

    async def _maybe_reconcile(self, accounts: set[str]) -> None:
        if self._reconcile is None:
            return
        now = time.monotonic()
        for account_id in sorted(accounts):
            if now - self._last_reconciled.get(account_id, -1e9) < self._interval:
                continue
            self._last_reconciled[account_id] = now
            try:
                await self._reconcile(account_id)
            except Exception:
                log.warning("usage reconcile failed for %s", account_id, exc_info=True)
                self._last_reconciled.pop(account_id, None)
