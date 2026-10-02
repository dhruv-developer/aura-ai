import httpx
import pytest

from aura_graph.tools import (
    CalculatorTool,
    HttpFetchTool,
    RemoteSandboxTool,
    SqlTool,
    extract_code,
    safe_eval,
)


def test_safe_eval() -> None:
    assert safe_eval("what is 17 * (3 + 4)?") == 119 and safe_eval("2^10") == 1024
    for bad in ['__import__("os").system("x")', "2**1000", "1/0", "a + 1", "(lambda: 1)()"]:
        with pytest.raises(Exception):
            safe_eval(bad)


async def test_calculator_tool() -> None:
    r = await CalculatorTool().run("acct", {"expression": "calculate 10 / 4"})
    assert r.ok and r.output == "2.5" and r.meta["no_llm"]
    assert not (await CalculatorTool().run("acct", {"expression": "open('x')"})).ok


async def test_http_fetch_blocks_private_addresses_and_http() -> None:
    async def private(host: str) -> list[str]:
        return ["169.254.169.254"]  # cloud metadata endpoint

    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html><script>x()</script><p>Hello <b>world</b></p></html>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        blocked = await HttpFetchTool(http, resolver=private).run(
            "a", {"url": "https://evil.example"}
        )
        assert not blocked.ok and "private" in blocked.output
        plain = await HttpFetchTool(http, resolver=public).run("a", {"url": "http://example.com"})
        assert not plain.ok
        ok = await HttpFetchTool(http, resolver=public).run("a", {"url": "https://example.com"})
        assert ok.ok and ok.output == "Hello world"
        allow = HttpFetchTool(http, resolver=public, allow_hosts={"docs.aura.ai"})
        assert not (await allow.run("a", {"url": "https://example.com"})).ok


async def test_sql_tool_is_read_only(pg_url: str) -> None:
    dsn = pg_url.replace("postgresql+asyncpg://", "postgresql://")

    async def dsn_for(account_id: str, name: str) -> str | None:
        return dsn if name == "default" else None

    tool = SqlTool(dsn_for, max_rows=2)
    r = await tool.run("acct", {"query": "SELECT code, audience FROM plans ORDER BY code"})
    assert r.ok and r.output.splitlines()[0] == "code,audience" and r.meta["truncated"]
    for bad in ["DELETE FROM plans", "SELECT 1; DROP TABLE plans", "UPDATE plans SET name='x'"]:
        assert not (await tool.run("acct", {"query": bad})).ok
    # Even a SELECT cannot write: the transaction is read-only.
    sneaky = await tool.run(
        "acct", {"query": "WITH x AS (DELETE FROM plans RETURNING 1) SELECT * FROM x"}
    )
    assert not sneaky.ok
    assert not (await tool.run("acct", {"query": "SELECT 1", "connection": "prod"})).ok


async def test_remote_sandbox_tool() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"stdout": "[1, 2, 3]\n", "stderr": "", "exit_code": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tool = RemoteSandboxTool(http, base_url="https://sandbox.internal", api_key="k")
        r = await tool.run(
            "acct_1", {"code": extract_code("```python\nprint(sorted([3,1,2]))\n```")}
        )
    assert r.ok and r.output.startswith("[1, 2, 3]")
    assert b"print(sorted" in seen[0].content and seen[0].url.path == "/execute"
