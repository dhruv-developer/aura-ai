import json

import httpx

from aura_api.app import create_app

CSRF = {"x-aura-csrf": "1"}


def _dataset() -> bytes:
    rows = [
        {
            "request": {
                "model": "stub/frontier",
                "messages": [{"role": "user", "content": f"q{i}"}],
            },
            "response": {
                "model": "stub/frontier",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [{"message": {"content": f"a{i}"}}],
            },
        }
        for i in range(10)
    ]
    return "\n".join(json.dumps(r) for r in rows).encode()


async def test_shadow_api_create_list_get_and_isolation(container) -> None:  # type: ignore[no-untyped-def]
    app = create_app(container, sync_catalog_on_startup=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            await c.post("/v1/auth/magic-link", json={"email": "ml@acme.io"})
            token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
            await c.post("/v1/auth/magic-link/verify", json={"token": token})
            org = (await c.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
            acct = org["account_id"]
            r = await c.post(
                f"/v1/accounts/{acct}/shadow-runs",
                files={"file": ("d.jsonl", _dataset(), "application/jsonl")},
                data={
                    "candidates": "stub/small, stub/medium",
                    "sample_size": "10",
                    "budget_usd": "2",
                },
                headers=CSRF,
            )
            assert r.status_code == 202, r.text
            run = r.json()
            assert run["status"] == "queued" and run["config"]["candidates"] == [
                "stub/small",
                "stub/medium",
            ]
            assert [x["id"] for x in (await c.get(f"/v1/accounts/{acct}/shadow-runs")).json()] == [
                run["id"]
            ]
            assert (await c.get(f"/v1/accounts/{acct}/shadow-runs/{run['id']}")).json()[
                "id"
            ] == run["id"]
            bad = await c.post(
                f"/v1/accounts/{acct}/shadow-runs",
                files={"file": ("d.jsonl", _dataset(), "application/jsonl")},
                data={"candidates": "stub/small", "budget_usd": "abc"},
                headers=CSRF,
            )
            assert bad.status_code == 422
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as eve:
            await eve.post("/v1/auth/magic-link", json={"email": "eve@evil.io"})
            token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
            await eve.post("/v1/auth/magic-link/verify", json={"token": token})
            assert (
                await eve.get(f"/v1/accounts/{acct}/shadow-runs/{run['id']}")
            ).status_code == 404
