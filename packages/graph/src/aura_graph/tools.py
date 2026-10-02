"""Tools for graph nodes (spec §52). Each is a port with safety built in.

Untrusted code never runs in-process: ``python`` is a remote-sandbox port only.
"""

from __future__ import annotations

import ast
import asyncio
import ipaddress
import operator
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from aura_shared.errors import ValidationFailed


@dataclass(frozen=True, slots=True)
class ToolResult:
    output: str
    ok: bool = True
    cost: Decimal = Decimal(0)
    meta: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult: ...


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ValidationFailed(f"tool {name!r} is not available")
        return tool

    def names(self) -> set[str]:
        return set(self._tools)


# ---- calculator (the "no LLM needed" path) ----------------------------------------------

_OPS: dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_PREFIX = re.compile(r"^\s*(what is|what's|calculate|compute|evaluate)\s*", re.I)


def safe_eval(expression: str) -> float | int:
    """Arithmetic only: whitelisted AST nodes, bounded exponents, no names or calls."""
    expr = _PREFIX.sub("", expression).replace("^", "**").rstrip(" ?=")
    if len(expr) > 200:
        raise ValidationFailed("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValidationFailed("not an arithmetic expression") from exc

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise ValidationFailed("exponent too large")
            return _OPS[type(node.op)](left, right)
        raise ValidationFailed("only numbers and + - * / // % ** are allowed")

    try:
        return ev(tree)  # type: ignore[no-any-return]
    except ZeroDivisionError as exc:
        raise ValidationFailed("division by zero") from exc


class CalculatorTool:
    name = "calculator"

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult:
        try:
            value = safe_eval(str(args.get("expression", "")))
        except ValidationFailed as exc:
            return ToolResult(str(exc), ok=False)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ToolResult(str(value), meta={"no_llm": True})


# ---- web search ---------------------------------------------------------------------------


class BraveSearchTool:
    name = "web_search"

    def __init__(self, http: httpx.AsyncClient, api_key: str, *, count: int = 5) -> None:
        self._http, self._key, self._count = http, api_key, count

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", ""))[:400]
        try:
            resp = await self._http.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": self._count},
                headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
                timeout=10,
            )
        except httpx.TransportError:
            return ToolResult("search unavailable", ok=False)
        if resp.status_code != 200:
            return ToolResult(f"search failed: HTTP {resp.status_code}", ok=False)
        results = resp.json().get("web", {}).get("results", [])[: self._count]
        lines = [
            f"- {r.get('title')} ({r.get('url')}): {r.get('description', '')}" for r in results
        ]
        return ToolResult("\n".join(lines) or "no results", meta={"results": len(results)})


# ---- safe HTTP fetch (SSRF-guarded) -------------------------------------------------------

_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
Resolver = Callable[[str], Awaitable[list[str]]]


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


class HttpFetchTool:
    """https only; every resolved address must be public (blocks 127/8, 10/8, 169.254/16
    metadata endpoints, ...). Production deployments should also route tool egress
    through a proxy, to close DNS-rebinding gaps between this check and the connect."""

    name = "http_fetch"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_bytes: int = 500_000,
        allow_hosts: set[str] | None = None,
        resolver: Resolver = _resolve,
    ) -> None:
        self._http, self._max, self._allow, self._resolve = http, max_bytes, allow_hosts, resolver

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url", ""))
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return ToolResult("only https URLs are allowed", ok=False)
        host = parsed.hostname
        if self._allow is not None and host not in self._allow:
            return ToolResult(f"host {host} is not on the allowlist", ok=False)
        try:
            ips = await self._resolve(host)
        except OSError:
            return ToolResult("host could not be resolved", ok=False)
        if not ips or not all(_is_public(ip) for ip in ips):
            return ToolResult("refusing to fetch a private or reserved address", ok=False)
        try:
            async with self._http.stream("GET", url, timeout=10, follow_redirects=False) as resp:
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self._max:
                        break
                    chunks.append(chunk)
        except httpx.TransportError:
            return ToolResult("fetch failed", ok=False)
        text = _TAGS.sub(" ", b"".join(chunks).decode("utf-8", "replace"))
        return ToolResult(
            re.sub(r"\s+", " ", text).strip()[:20_000], meta={"status": resp.status_code}
        )


# ---- read-only SQL --------------------------------------------------------------------------

_SQL_OK = re.compile(r"^\s*(select|with)\b", re.I)
_SQL_BAD = re.compile(r";\s*\S", re.S)  # a second statement


class SqlTool:
    """Read-only queries against *tenant-configured* connections only (never a DSN taken
    from the request: that would be SSRF into internal databases)."""

    name = "sql"

    def __init__(
        self,
        dsn_for: Callable[[str, str], Awaitable[str | None]],
        *,
        max_rows: int = 200,
        timeout_ms: int = 5_000,
    ) -> None:
        self._dsn_for, self._max_rows, self._timeout = dsn_for, max_rows, timeout_ms

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult:
        import asyncpg

        query, connection = str(args.get("query", "")), str(args.get("connection", "default"))
        if not _SQL_OK.match(query) or _SQL_BAD.search(query):
            return ToolResult("only a single SELECT/WITH statement is allowed", ok=False)
        dsn = await self._dsn_for(account_id, connection)
        if dsn is None:
            return ToolResult(f"no database connection named {connection!r}", ok=False)
        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = {int(self._timeout)}")
                # Tenant SQL is executed by design: it is restricted to a single
                # SELECT/WITH, runs on a tenant-configured connection, inside a read-only
                # transaction with a statement timeout.
                rows = await conn.fetch(
                    f"SELECT * FROM ({query.rstrip(' ;')}) q LIMIT {self._max_rows + 1}"  # noqa: S608
                )
        except asyncpg.PostgresError as exc:
            return ToolResult(f"query failed: {exc.__class__.__name__}", ok=False)
        finally:
            await conn.close()
        truncated = len(rows) > self._max_rows
        rows = rows[: self._max_rows]
        if not rows:
            return ToolResult("(no rows)", meta={"rows": 0})
        cols = list(rows[0].keys())
        lines = [",".join(cols)] + [",".join(str(r[c]) for c in cols) for r in rows]
        return ToolResult("\n".join(lines), meta={"rows": len(rows), "truncated": truncated})


# ---- python (remote sandbox only) ------------------------------------------------------------

_CODE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    m = _CODE.search(text)
    return (m.group(1) if m else text).strip()


class RemoteSandboxTool:
    """POSTs code to an isolated sandbox service (Firecracker / E2B-style). The sandbox
    owns isolation, CPU/memory/time limits and network policy."""

    name = "python"

    def __init__(
        self, http: httpx.AsyncClient, *, base_url: str, api_key: str, timeout_s: int = 30
    ) -> None:
        self._http, self._url, self._key, self._timeout = (
            http,
            base_url.rstrip("/"),
            api_key,
            timeout_s,
        )

    async def run(self, account_id: str, args: dict[str, Any]) -> ToolResult:
        code = str(args.get("code", ""))
        if not code.strip():
            return ToolResult("no code to run", ok=False)
        try:
            resp = await self._http.post(
                f"{self._url}/execute",
                json={"code": code, "timeout_s": self._timeout, "tenant": account_id},
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout + 5,
            )
        except httpx.TransportError:
            return ToolResult("sandbox unavailable", ok=False)
        if resp.status_code != 200:
            return ToolResult(f"sandbox error: HTTP {resp.status_code}", ok=False)
        d = resp.json()
        ok = int(d.get("exit_code", 1)) == 0
        out = (d.get("stdout") or "") + (f"\nstderr:\n{d['stderr']}" if d.get("stderr") else "")
        return ToolResult(
            out[:20_000] or "(no output)", ok=ok, meta={"exit_code": d.get("exit_code")}
        )
