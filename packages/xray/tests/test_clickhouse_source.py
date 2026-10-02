import json
from datetime import UTC, datetime

import httpx

from aura_xray.adapters.clickhouse_source import ClickHouseTrafficSource


async def test_query_uses_server_side_parameters() -> None:
    seen: list[httpx.Request] = []
    row = {
        "occurred_at": "2026-09-30 10:00:00.000",
        "model": "anthropic/claude-opus-5-5",
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "total_latency_ms": 900,
        "total_cost_usd": "0.00008",
        "status": "succeeded",
        "routing_features": json.dumps({"complexity": 0.2}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=json.dumps(row) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        src = ClickHouseTrafficSource(http, "http://ch:8123")
        recs = await src.fetch(
            "acct_1'; DROP TABLE x;--",
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 10, 1, tzinfo=UTC),
        )
    [req] = seen
    assert req.url.params["param_account"] == "acct_1'; DROP TABLE x;--"
    assert "DROP" not in req.content.decode()  # tenant input is never interpolated into SQL
    assert recs[0].model == "claude-opus-5-5" and recs[0].features == {"complexity": 0.2}
    assert recs[0].status == "ok"
