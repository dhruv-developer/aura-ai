"""Sinks for telemetry batches."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


class TelemetrySink(Protocol):
    async def write(
        self, executions: list[dict[str, Any]], calls: list[dict[str, Any]]
    ) -> None: ...


class ClickHouseSink:
    """Batched inserts over the ClickHouse HTTP interface (JSONEachRow).

    Tables are ReplacingMergeTree keyed on event_id, so a redelivered batch
    deduplicates on merge (at-least-once delivery → effectively-once storage).
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        *,
        database: str = "aura",
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._db = database
        self._auth: httpx.BasicAuth | None = httpx.BasicAuth(user, password or "") if user else None

    async def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        body = "\n".join(json.dumps(r, default=str) for r in rows).encode()
        resp = await self._http.post(
            self._url,
            params={"query": f"INSERT INTO {self._db}.{table} FORMAT JSONEachRow"},
            content=body,
            auth=self._auth or httpx.USE_CLIENT_DEFAULT,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"clickhouse insert into {table} failed: {resp.status_code} {resp.text[:300]}"
            )

    async def write(self, executions: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        await self._insert("executions", executions)
        await self._insert("execution_calls", calls)


class InMemorySink:
    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    async def write(self, executions: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self.executions.extend(executions)
        self.calls.extend(calls)
