"""HTTP-level tests through the real app (ASGI transport, real Postgres + Redis)."""

from collections.abc import AsyncIterator

import httpx
import pytest

from aura_api.app import create_app

CSRF = {"x-aura-csrf": "1"}


@pytest.fixture
async def client(container) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(container, sync_catalog_on_startup=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
            yield c


async def _login(client: httpx.AsyncClient, container, email: str) -> None:  # type: ignore[no-untyped-def]
    r = await client.post("/v1/auth/magic-link", json={"email": email})
    assert r.status_code == 202, r.text
    token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
    r = await client.post("/v1/auth/magic-link/verify", json={"token": token})
    assert r.status_code == 200, r.text
    assert "httponly" in r.headers["set-cookie"].lower()


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"ok": True}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["postgres"] is True


async def test_login_me_org_and_api_key_flow(client: httpx.AsyncClient, container) -> None:  # type: ignore[no-untyped-def]
    assert (await client.get("/v1/me")).status_code == 401
    await _login(client, container, "founder@acme.io")

    me = (await client.get("/v1/me")).json()
    assert me["user"]["email"] == "founder@acme.io"
    assert [a["kind"] for a in me["accounts"]] == ["individual"]

    # CSRF: cookie-authenticated writes need the custom header.
    assert (await client.post("/v1/organizations", json={"name": "Acme"})).status_code == 403
    org = (await client.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
    acct = org["account_id"]

    created = await client.post(
        f"/v1/accounts/{acct}/api-keys",
        json={"name": "prod", "environment_id": org["environment_ids"]["production"]},
        headers=CSRF,
    )
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key.startswith("aura_sk_prod_")
    assert sorted(created.json()["permissions"]) == ["runtime:execute", "telemetry:write"]
    listed = (await client.get(f"/v1/accounts/{acct}/api-keys")).json()
    assert "key" not in listed[0]  # plaintext is never shown again

    # The machine principal only holds its granted permissions.
    bearer = {"authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=client._transport, base_url="http://api.test") as bot:
        r = await bot.get(f"/v1/accounts/{acct}/api-keys", headers=bearer)
        assert r.status_code == 403
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["code"] == "forbidden"
        assert (await bot.get("/v1/me", headers=bearer)).status_code == 403  # users only
        bad = {"authorization": "Bearer aura_sk_prod_aaaaaaaaaaaa_" + "x" * 43}
        assert (await bot.get(f"/v1/accounts/{acct}/api-keys", headers=bad)).status_code == 401

    subscription = (await client.get(f"/v1/accounts/{acct}/subscription")).json()
    assert subscription["plan_code"] == "startup" and subscription["status"] == "trialing"
    ents = {e["feature"]: e for e in (await client.get(f"/v1/accounts/{acct}/entitlements")).json()}
    assert ents["shadow_optimization"]["enabled"] and ents["executions"]["used"] == 0

    key_id = listed[0]["id"]
    r = await client.delete(f"/v1/accounts/{acct}/api-keys/{key_id}", headers=CSRF)
    assert r.status_code == 204
    async with httpx.AsyncClient(transport=client._transport, base_url="http://api.test") as bot:
        r = await bot.get(f"/v1/accounts/{acct}/api-keys", headers=bearer)
        assert r.status_code == 401 and "revoked" in r.json()["detail"]

    assert (await client.post("/v1/auth/logout", headers=CSRF)).status_code == 204
    assert (await client.get("/v1/me")).status_code == 401


async def test_cross_tenant_access_is_not_found(client: httpx.AsyncClient, container) -> None:  # type: ignore[no-untyped-def]
    await _login(client, container, "a@acme.io")
    org = (await client.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
    victim = org["account_id"]

    transport = client._transport
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as eve:
        await _login(eve, container, "eve@evil.io")
        for method, path in [
            ("GET", f"/v1/accounts/{victim}/api-keys"),
            ("GET", f"/v1/accounts/{victim}/subscription"),
            ("GET", f"/v1/accounts/{victim}/entitlements"),
            ("GET", f"/v1/accounts/{victim}/credits"),
            ("POST", f"/v1/accounts/{victim}/api-keys"),
        ]:
            r = await eve.request(method, path, headers=CSRF, json={"name": "x"})
            assert r.status_code == 404, (method, path, r.status_code)


async def test_plans_are_public_and_problem_json_on_validation(client: httpx.AsyncClient) -> None:
    plans = (await client.get("/v1/plans", params={"audience": "b2c"})).json()
    assert [p["code"] for p in plans] == ["free", "plus", "pro"]
    assert "enterprise" not in {p["code"] for p in (await client.get("/v1/plans")).json()}
    r = await client.post("/v1/auth/magic-link", json={"email": "not-an-email"})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"


async def test_webhook_requires_valid_signature(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/webhooks/razorpay", content=b"{}", headers={"x-razorpay-signature": "nope"}
    )
    assert r.status_code == 401
