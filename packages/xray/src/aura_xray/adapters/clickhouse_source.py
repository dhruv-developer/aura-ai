"""Gateway traffic → TrafficRecords, read from ClickHouse (for customers on the runtime).

Prompt text is not in ClickHouse. Records carry the routing features the runtime logged,
so complexity-based detectors still work, while content-based ones (caching) do not.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import httpx

from aura_xray.domain.parsers import _ts
from aura_xray.domain.records import TrafficRecord

QUERY = """
SELECT occurred_at, model, input_tokens, output_tokens, cached_input_tokens,
       total_latency_ms, total_cost_usd, status, routing_features
FROM aura.executions FINAL
WHERE account_id = {account:String}
  AND occurred_at >= {start:DateTime64(3)} AND occurred_at < {end:DateTime64(3)}
  AND status IN ('succeeded', 'failed')
ORDER BY occurred_at
LIMIT {limit:UInt32}
FORMAT JSONEachRow
"""


class ClickHouseTrafficSource:
    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url.rstrip("/")

    async def fetch(
        self, account_id: str, start: datetime, end: datetime, *, limit: int = 200_000
    ) -> list[TrafficRecord]:
        # Server-side parameter binding: no string interpolation of tenant input.
        resp = await self._http.post(
            self._url,
            params={
                "param_account": account_id,
                "param_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "param_end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "param_limit": str(limit),
            },
            content=QUERY.encode(),
            timeout=60,
        )
        resp.raise_for_status()
        out = []
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            features = json.loads(r.get("routing_features") or "{}")
            out.append(
                TrafficRecord(
                    ts=_ts(r["occurred_at"]),
                    model=str(r["model"]).split("/", 1)[-1],
                    input_tokens=int(r["input_tokens"]),
                    output_tokens=int(r["output_tokens"]),
                    cached_input_tokens=int(r.get("cached_input_tokens") or 0),
                    latency_ms=int(r["total_latency_ms"]),
                    cost_usd=Decimal(str(r["total_cost_usd"])),
                    status="ok" if r["status"] == "succeeded" else "error",
                    features=features,
                )
            )
        return out
