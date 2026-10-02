"""Members and custom roles over HTTP."""

from collections.abc import AsyncIterator

import httpx
import pytest

from aura_api.app import create_app

CSRF = {"x-aura-csrf": "1"}


@pytest.fixture
async def app(container):  # type: ignore[no-untyped-def]
    a = create_app(container, sync_catalog_on_startup=False)
    async with a.router.lifespan_context(a):
        yield a


async def _client(app, container, email: str) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")
    c.cookies.set(container.settings.session_cookie_name, await container.sessions.create(user.id))
    return c


@pytest.fixture
async def owner(app, container) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:  # type: ignore[no-untyped-def]
    c = await _client(app, container, "owner@acme.io")
    org = (await c.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
    yield c, org["account_id"]
    await c.aclose()


async def test_member_lifecycle_and_escalation_guards(app, container, owner) -> None:  # type: ignore[no-untyped-def]
    c, acct = owner
    base = f"/v1/accounts/{acct}"
    r = await c.post(
        f"{base}/members", json={"email": "Admin@Acme.io", "role": "admin"}, headers=CSRF
    )
    assert r.status_code == 201, r.text
    admin_id = r.json()["user_id"]
    assert r.json()["email"] == "admin@acme.io"
    dup = await c.post(f"{base}/members", json={"email": "admin@acme.io"}, headers=CSRF)
    assert dup.status_code == 409

    admin = await _client(app, container, "admin@acme.io")
    try:
        # Admins manage members but cannot mint owners or touch owners.
        r = await admin.post(
            f"{base}/members", json={"email": "x@acme.io", "role": "owner"}, headers=CSRF
        )
        assert r.status_code == 403
        r = await admin.post(
            f"{base}/members", json={"email": "eng@acme.io", "role": "engineer"}, headers=CSRF
        )
        assert r.status_code == 201
        owner_id = next(
            m["user_id"]
            for m in (await admin.get(f"{base}/members")).json()
            if m["role"] == "owner"
        )
        assert (await admin.delete(f"{base}/members/{owner_id}", headers=CSRF)).status_code == 403
    finally:
        await admin.aclose()

    # The last owner cannot leave or be demoted.
    owner_id = next(
        m["user_id"] for m in (await c.get(f"{base}/members")).json() if m["role"] == "owner"
    )
    r = await c.patch(f"{base}/members/{owner_id}", json={"role": "admin"}, headers=CSRF)
    assert r.status_code == 409
    assert (await c.delete(f"{base}/members/{admin_id}", headers=CSRF)).status_code == 204
    assert [m["email"] for m in (await c.get(f"{base}/members")).json()] == [
        "eng@acme.io",
        "owner@acme.io",
    ]


async def test_custom_roles_need_entitlement_and_shape_access(app, container, owner) -> None:  # type: ignore[no-untyped-def]
    c, acct = owner
    base = f"/v1/accounts/{acct}"
    role = {"name": "Auditor", "permissions": ["account:read", "executions:read", "billing:read"]}
    assert (
        await c.post(f"{base}/roles", json=role, headers=CSRF)
    ).status_code == 403  # startup plan
    await container.subscriptions.change_plan(
        account_id=acct, plan_code="growth", actor_type="t", actor_id="t", reason="t"
    )
    bad = {"name": "Boss", "permissions": ["account:manage"]}
    assert (await c.post(f"{base}/roles", json=bad, headers=CSRF)).status_code == 422
    r = await c.post(f"{base}/roles", json=role, headers=CSRF)
    assert r.status_code == 201, r.text
    role_id = r.json()["id"]
    r = await c.post(
        f"{base}/members",
        json={"email": "aud@acme.io", "role": "viewer", "custom_role_id": role_id},
        headers=CSRF,
    )
    assert r.status_code == 201

    aud = await _client(app, container, "aud@acme.io")
    try:
        access = next(
            a for a in (await aud.get("/v1/me")).json()["accounts"] if a["account_id"] == acct
        )
        assert access["role"] == "custom:Auditor"
        assert sorted(access["permissions"]) == sorted(role["permissions"])
        assert (await aud.get(f"{base}/subscription")).status_code == 200  # billing:read
        assert (await aud.get(f"{base}/members")).status_code == 403  # not in the role
        # Deleting the role drops them back to their base role (viewer).
        assert (await c.delete(f"{base}/roles/{role_id}", headers=CSRF)).status_code == 204
        assert (await aud.get(f"{base}/subscription")).status_code == 403
        assert (await aud.get(f"{base}/members")).status_code == 200
    finally:
        await aud.aclose()
