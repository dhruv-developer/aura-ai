import contextlib
import json
from decimal import Decimal

import httpx

from aura_shared.events import ExecutionCompleted, ExecutionStatus, ModelCallRecord, RoutingDecision
from aura_workers.sinks import ClickHouseSink, InMemorySink
from aura_workers.telemetry import TelemetryProcessor


def _envelope(account: str = "acct_1", status=ExecutionStatus.SUCCEEDED) -> dict:  # type: ignore[no-untyped-def,type-arg]
    evt = ExecutionCompleted(
        account_id=account,
        region="in",
        trace_id="trc_1",
        execution_id="exe_1",
        workspace_id="ws_1",
        status=status,
        total_latency_ms=120,
        total_cost=Decimal("0.0012"),
        calls=[
            ModelCallRecord(
                provider="stub",
                model="stub/small",
                input_tokens=0,
                output_tokens=0,
                latency_ms=5,
                cost=Decimal(0),
                error="overloaded",
            ),
            ModelCallRecord(
                provider="stub",
                model="stub/medium",
                input_tokens=10,
                output_tokens=4,
                latency_ms=90,
                cost=Decimal("0.0012"),
            ),
        ],
        routing=RoutingDecision(
            router_version="rules-v1",
            chosen_model="stub/small",
            candidates=["stub/small", "stub/medium"],
            features={"est_input_tokens": 10},
            reason="auto",
        ),
        notes=["n1"],
    )
    return json.loads(json.dumps(evt.envelope(), default=str))


async def test_processor_writes_rows_dedupes_and_reconciles(redis_client) -> None:  # type: ignore[no-untyped-def]
    sink, reconciled = InMemorySink(), []

    async def reconcile(account_id: str) -> None:
        reconciled.append(account_id)

    p = TelemetryProcessor(
        sink=sink, state=redis_client, reconcile=reconcile, reconcile_interval_s=60
    )
    env = _envelope()
    await p.process([env, env, {"type": "something.else", "data": {"event_id": "x"}}])
    assert p.processed == 1 and p.duplicates == 1 and p.skipped == 1
    [row] = sink.executions
    assert row["model"] == "stub/medium" and row["attempts"] == 2 and row["input_tokens"] == 10
    assert row["total_cost_usd"] == "0.0012" and json.loads(row["routing_features"]) == {
        "est_input_tokens": 10
    }
    assert [c["error"] for c in sink.calls] == ["overloaded", None]

    await p.process([env])  # redelivery after ack loss
    assert p.duplicates == 2 and len(sink.executions) == 1
    await p.process([_envelope()])  # new event, same account: reconcile is debounced
    assert reconciled == ["acct_1"]


async def test_failed_sink_write_does_not_mark_events_seen(redis_client) -> None:  # type: ignore[no-untyped-def]
    class Broken:
        async def write(self, executions, calls):  # type: ignore[no-untyped-def]
            raise RuntimeError("clickhouse down")

    env = _envelope()
    p = TelemetryProcessor(sink=Broken(), state=redis_client)
    with contextlib.suppress(RuntimeError):
        await p.process([env])
    good = InMemorySink()
    await TelemetryProcessor(sink=good, state=redis_client).process([env])
    assert len(good.executions) == 1  # redelivery is processed, not dropped


async def test_clickhouse_sink_sends_json_each_row() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sink = ClickHouseSink(http, "http://ch:8123/", user="aura", password="pw")
        await sink.write([{"event_id": "e1"}, {"event_id": "e2"}], [])
    [req] = seen
    assert req.url.params["query"] == "INSERT INTO aura.executions FORMAT JSONEachRow"
    assert req.content.decode().splitlines() == ['{"event_id": "e1"}', '{"event_id": "e2"}']
    assert req.headers["authorization"].startswith("Basic ")
