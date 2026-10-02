import asyncio
import time

import httpx
import pytest
from sqlalchemy import update

from aura_api.app import create_app
from aura_auth.domain import totp
from aura_db.models import User

CSRF = {"x-aura-csrf": "1"}


@pytest.fixture
async def app(container):  # type: ignore[no-untyped-def]
    a = create_app(container, sync_catalog_on_startup=False)
    async with a.router.lifespan_context(a):
        yield a


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


async def _login(c, container, email) -> None:  # type: ignore[no-untyped-def]
    await c.post("/v1/auth/magic-link", json={"email": email})
    token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
    assert (await c.post("/v1/auth/magic-link/verify", json={"token": token})).status_code == 200


async def _admin(c, container) -> None:  # type: ignore[no-untyped-def]
    await _login(c, container, "root@aura.ai")
    async with container.db.system() as s:
        await s.execute(update(User).where(User.email == "root@aura.ai").values(is_superadmin=True))
    secret = (await c.post("/v1/admin/auth/totp/enroll", headers=CSRF)).json()["secret"]
    code = totp.code_at(secret, int(time.time() // 30))
    r = await c.post("/v1/admin/auth/totp/verify", json={"code": code}, headers=CSRF)
    assert r.status_code == 204, r.text
    replay = await c.post("/v1/admin/auth/totp/verify", json={"code": code}, headers=CSRF)
    assert replay.status_code == 401  # one-time codes cannot be replayed
    # httpx keeps the path-scoped admin cookie for /v1/admin requests.


async def test_admin_requires_superadmin_and_second_factor(app, container) -> None:  # type: ignore[no-untyped-def]
    async with _client(app) as c:
        await _login(c, container, "user@acme.io")
        assert (await c.get("/v1/admin/metrics")).status_code == 403
        assert (await c.post("/v1/admin/auth/totp/enroll", headers=CSRF)).status_code == 403
    async with _client(app) as c:
        await _login(c, container, "root@aura.ai")
        async with container.db.system() as s:
            await s.execute(
                update(User).where(User.email == "root@aura.ai").values(is_superadmin=True)
            )
        assert (await c.get("/v1/admin/metrics")).status_code == 401  # no TOTP session yet


async def test_metrics_actions_and_audit(app, container) -> None:  # type: ignore[no-untyped-def]
    async with _client(app) as user:
        await _login(user, container, "founder@acme.io")
        org = (await user.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
    async with _client(app) as c:
        await _admin(c, container)
        me = (await c.get("/v1/me")).json()
        personal = next(a["account_id"] for a in me["accounts"] if a["kind"] == "individual")
        r = await c.post(
            f"/v1/admin/accounts/{personal}/plan",
            json={"plan": "plus", "reason": "creator program"},
            headers=CSRF,
        )
        assert r.status_code == 200 and r.json()["plan"] == "plus"

        m = (await c.get("/v1/admin/metrics")).json()
        assert m["organizations"] == 1 and m["b2c_accounts"] == 2 and m["users"] == 2
        assert m["mrr_inr"]["b2c"] == "499.00" and m["mrr_inr"]["b2b"] == "0.00"  # trial ≠ revenue
        assert m["arr_inr"] == "5988.00"

        listed = (await c.get("/v1/admin/accounts", params={"q": "acme"})).json()
        assert len(listed) == 2  # the org and its founder's personal account (email match)
        listed = (
            await c.get("/v1/admin/accounts", params={"q": "acme", "kind": "organization"})
        ).json()
        assert [a["account_id"] for a in listed] == [org["account_id"]]
        assert listed[0]["plan"] == "startup"

        r = await c.post(
            f"/v1/admin/accounts/{org['account_id']}/credits",
            json={"amount": 500, "reason": "pilot credit", "reference": "pilot-1"},
            headers=CSRF,
        )
        assert r.json()["balance"] == 500
        r = await c.post(
            f"/v1/admin/accounts/{org['account_id']}/overrides",
            json={"feature": "shadow_runs", "limit": 999_999, "period": "month", "reason": "pilot"},
            headers=CSRF,
        )
        assert r.status_code == 201
        detail = (await c.get(f"/v1/admin/accounts/{org['account_id']}")).json()
        assert detail["credits_balance"] == 500
        assert detail["usage"]["shadow_runs"]["limit"] == 999_999
        assert detail["members"][0]["email"] == "founder@acme.io"
        health = (await c.get("/v1/admin/optimization-health")).json()
        assert health["rollback_rate"] == 0.0 and "deployments_by_status" in health
        actions = {a["action"] for a in (await c.get("/v1/admin/audit")).json()}
        assert {
            "credits.grant",
            "entitlement.override",
            "subscription.change_plan",
            "admin.login",
        } <= actions


async def test_suspension_cuts_off_runtime_keys_immediately(app, container, runtime) -> None:  # type: ignore[no-untyped-def]
    from aura_runtime.app import create_app as create_runtime

    user = await container.identity.login_with_email("dev@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    access = await container.identity.resolve_access(user.id, org.account_id)
    _, key = await container.api_keys.issue(
        account_id=org.account_id,
        actor_user_id=user.id,
        actor_permissions=access.permissions,
        name="k",
        permissions={"runtime:execute"},
        environment_id=org.environment_ids["production"],
    )
    rt = create_runtime(runtime)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    async with (
        rt.router.lifespan_context(rt),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=rt), base_url="http://rt") as r_client,
    ):
        auth = {"authorization": f"Bearer {key}"}
        assert (
            await r_client.post("/v1/chat/completions", json=body, headers=auth)
        ).status_code == 200
        admin = _client(app)
        await _admin(admin, container)
        r = await admin.post(
            f"/v1/admin/accounts/{org.account_id}/suspend",
            json={"reason": "abuse report"},
            headers=CSRF,
        )
        assert r.status_code == 204
        # The runtime pod's L1 is invalidated via pub/sub; the next call is rejected.
        for _ in range(50):
            resp = await r_client.post("/v1/chat/completions", json=body, headers=auth)
            if resp.status_code == 403:
                break
            await asyncio.sleep(0.02)
        assert resp.status_code == 403 and "suspended" in resp.json()["detail"]
        await admin.post(
            f"/v1/admin/accounts/{org.account_id}/restore",
            json={"reason": "resolved"},
            headers=CSRF,
        )
        await admin.aclose()
        for _ in range(50):
            resp = await r_client.post("/v1/chat/completions", json=body, headers=auth)
            if resp.status_code == 200:
                break
            await asyncio.sleep(0.02)
        assert resp.status_code == 200
