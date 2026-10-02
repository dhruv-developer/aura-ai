"""Telemetry publishing port and adapters. Publishing never blocks or fails a request."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import Protocol

from aura_shared.events import Event, subject_for

log = logging.getLogger("aura.engine.telemetry")


class TelemetryPublisher(Protocol):
    def publish(self, event: Event) -> None:
        """Enqueue and return immediately."""
        ...

    async def close(self) -> None: ...


class InMemoryPublisher:
    """Tests and local runs: keeps the most recent events in a bounded ring buffer."""

    def __init__(self, *, maxlen: int = 10_000) -> None:
        self.events: deque[Event] = deque(maxlen=maxlen)

    def publish(self, event: Event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        return None


class NatsPublisher:
    """JetStream publisher behind a bounded in-memory queue.

    ``Nats-Msg-Id`` = ``event_id`` enables JetStream's duplicate window, so a retried
    publish is stored once. If NATS is unreachable the queue fills and the oldest events
    are dropped and counted. Telemetry loss is preferable to request latency; quotas and
    billing do not depend on it.
    """

    def __init__(self, url: str, *, queue_size: int = 10_000) -> None:
        self._url = url
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.published = 0

    def publish(self, event: Event) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    async def _run(self) -> None:
        import nats  # local import: optional at test time

        backoff = 0.5
        while True:
            try:
                nc = await nats.connect(self._url, max_reconnect_attempts=-1)
                js = nc.jetstream()
                backoff = 0.5
                while True:
                    event = await self._queue.get()
                    await js.publish(
                        subject_for(event),
                        json.dumps(event.envelope(), default=str).encode(),
                        headers={"Nats-Msg-Id": event.event_id},
                    )
                    self.published += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("telemetry publisher error; retrying", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
