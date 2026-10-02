"""JetStream pull consumer driving TelemetryProcessor (at-least-once)."""

from __future__ import annotations

import asyncio
import json
import logging

from aura_workers.telemetry import TelemetryProcessor

log = logging.getLogger("aura.workers.nats")

STREAM = "AURA_EXEC"
SUBJECTS = ["aura.exec.>"]
DURABLE = "telemetry-clickhouse"


async def ensure_stream(js: object) -> None:
    from nats.js.api import RetentionPolicy, StreamConfig
    from nats.js.errors import NotFoundError

    try:
        await js.stream_info(STREAM)  # type: ignore[attr-defined]
    except NotFoundError:
        await js.add_stream(  # type: ignore[attr-defined]
            StreamConfig(
                name=STREAM,
                subjects=SUBJECTS,
                retention=RetentionPolicy.LIMITS,
                max_age=7 * 24 * 3600,  # replay window for reprocessing
                duplicate_window=120,  # Nats-Msg-Id dedupe on publish
            )
        )


async def run(url: str, processor: TelemetryProcessor, *, batch: int = 500) -> None:
    import nats
    from nats.errors import TimeoutError as NatsTimeout
    from nats.js.api import AckPolicy, ConsumerConfig

    nc = await nats.connect(url, max_reconnect_attempts=-1)
    js = nc.jetstream()
    await ensure_stream(js)
    sub = await js.pull_subscribe(
        "aura.exec.>",
        durable=DURABLE,
        config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, max_deliver=10, ack_wait=60),
    )
    log.info("telemetry consumer started")
    while True:
        try:
            msgs = await sub.fetch(batch, timeout=2)
        except NatsTimeout:
            continue
        envelopes = []
        for m in msgs:
            try:
                envelopes.append(json.loads(m.data))
            except ValueError:
                log.error("dropping undecodable telemetry message")
                await m.term()
        try:
            await processor.process(envelopes)
        except Exception:
            log.exception("telemetry batch failed; will be redelivered")
            for m in msgs:
                await m.nak(delay=5)
            await asyncio.sleep(1)
            continue
        for m in msgs:
            await m.ack()
