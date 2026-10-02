"""AURA Personal through the runtime: web-session access, credits, memory, sharing."""

from collections.abc import AsyncIterator

import httpx
import pytest

from aura_api.app import create_app as create_api
from aura_billing.domain.ledger import TxnKind
from aura_runtime.app import create_app

CSRF = {"x-aura-csrf": "1"}
HINDI = {"content": "I prefer answers in Hindi"}
MSG = {"model": "aura/auto", "messages": [{"role": "user", "content": "hello there"}]}


@pytest.fixture
async def rt(runtime) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt.test") as c,
    ):
        yield c


@pytest.fixture
async def api(container) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_api(container, sync_catalog_on_startup=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as c,
    ):
        yield c


async def _signed_in(container, email: str):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    [access] = await container.identity.list_access(user.id)
    cookie = await container.sessions.create(user.id)
    return user.id, access.account_id, {container.settings.session_cookie_name: cookie}


async def test_session_cookie_runs_free_plan_without_api_access(rt, container) -> None:  # type: ignore[no-untyped-def]
    _, acct, cookies = await _signed_in(container, "me@x.io")
    rt.cookies.update(cookies)
    assert (await rt.post("/v1/chat/completions", json=MSG)).status_code == 403  # no CSRF
    r = await rt.post("/v1/chat/completions", json=MSG, headers=CSRF)
    assert r.status_code == 200, r.text
    aura = r.json()["aura"]
    assert aura["credential_source"] == "platform"
    assert float(aura["baseline_cost_usd"]) >= float(aura["cost_usd"])
    assert await container.credits.balance(acct) < 1000  # charged in credits

    rt.cookies.clear()
    rt.cookies.update({container.settings.session_cookie_name: "usr_x.forged"})
    assert (await rt.post("/v1/chat/completions", json=MSG, headers=CSRF)).status_code == 401


async def test_org_access_via_header_needs_membership(rt, container) -> None:  # type: ignore[no-untyped-def]
    owner = await container.identity.login_with_email("own@acme.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    _, _, eve = await _signed_in(container, "eve@evil.io")
    rt.cookies.update(eve)
    r = await rt.post(
        "/v1/chat/completions", json=MSG, headers={**CSRF, "x-aura-account": org.account_id}
    )
    assert r.status_code == 404


async def test_out_of_credits_blocks_platform_keys(rt, container) -> None:  # type: ignore[no-untyped-def]
    _, acct, cookies = await _signed_in(container, "broke@x.io")
    await container.credits.post(acct, TxnKind.CONSUME, 1000)
    await container.credit_gate.invalidate(acct)
    rt.cookies.update(cookies)
    r = await rt.post("/v1/chat/completions", json=MSG, headers=CSRF)
    assert r.status_code == 402 and "out of credits" in r.json()["detail"]


async def test_personal_memory_is_private_and_editable(rt, container) -> None:  # type: ignore[no-untyped-def]
    user_id, acct, cookies = await _signed_in(container, "mem@x.io")
    rt.cookies.update(cookies)
    r = await rt.post("/v1/me/memories", json=HINDI, headers=CSRF)
    assert r.status_code == 403  # free plan has no personal memory
    await container.subscriptions.change_plan(
        account_id=acct, plan_code="plus", actor_type="test", actor_id="t", reason="test"
    )
    r = await rt.post("/v1/me/memories", json=HINDI, headers=CSRF)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    listed = (await rt.get("/v1/me/memories")).json()["data"]
    assert [m["content"] for m in listed] == ["I prefer answers in Hindi"]

    chat = await rt.post(
        "/v1/chat/completions",
        json={"model": "aura/auto", "messages": [{"role": "user", "content": "answers please"}]},
        headers=CSRF,
    )
    assert any(n.startswith("memory: 1 items") for n in chat.json()["aura"]["notes"])
    other = await rt.post(
        "/v1/chat/completions",
        json={**MSG, "aura": {"memory": ["user:usr_someone_else"]}},
        headers=CSRF,
    )
    assert other.status_code == 403

    assert (await rt.delete(f"/v1/me/memories/{mid}", headers=CSRF)).status_code == 204
    assert (await rt.get("/v1/me/memories")).json()["data"] == []
    assert user_id


async def test_share_cards_are_content_free(rt, api, container) -> None:  # type: ignore[no-untyped-def]
    _, acct, cookies = await _signed_in(container, "share@x.io")
    rt.cookies.update(cookies)
    api.cookies.update(cookies)
    secret = "my secret medical question"
    r = await rt.post(
        "/v1/chat/completions",
        json={"model": "aura/auto", "messages": [{"role": "user", "content": secret}]},
        headers=CSRF,
    )
    exe = r.json()["aura"]["execution_id"]
    card = await api.post(f"/v1/accounts/{acct}/shares", json={"execution_id": exe}, headers=CSRF)
    assert card.status_code == 201, card.text
    again = await api.post(f"/v1/accounts/{acct}/shares", json={"execution_id": exe}, headers=CSRF)
    assert again.json()["id"] == card.json()["id"]  # idempotent

    async with httpx.AsyncClient(transport=api._transport, base_url="http://api.test") as anon:
        public = await anon.get(f"/v1/shares/{card.json()['id']}")
    assert public.status_code == 200
    body = public.text
    assert secret not in body and "[small]" not in body and "execution_id" not in public.json()
    assert public.json()["model"] == "stub/small" and public.json()["saved_pct"] >= 0

    missing = await api.post(
        f"/v1/accounts/{acct}/shares", json={"execution_id": "exe_nope"}, headers=CSRF
    )
    assert missing.status_code == 404
    gone = await api.delete(f"/v1/accounts/{acct}/shares/{card.json()['id']}", headers=CSRF)
    assert gone.status_code == 204


async def test_referral_and_packs_http(api, container) -> None:  # type: ignore[no-untyped-def]
    _, _, ref_cookies = await _signed_in(container, "ref@x.io")
    api.cookies.update(ref_cookies)
    code = (await api.get("/v1/me/referral")).json()["code"]
    packs = (await api.get("/v1/credit-packs")).json()
    assert [p["code"] for p in packs] == ["small", "medium", "large"]
    _, acct, new_cookies = await _signed_in(container, "new@x.io")
    api.cookies.clear()
    api.cookies.update(new_cookies)
    r = await api.post("/v1/me/referral/redeem", json={"code": code}, headers=CSRF)
    assert r.json() == {"credits": 2000}
    buy = f"/v1/accounts/{acct}/credits/purchase"
    r = await api.post(buy, json={"pack": "small"}, headers=CSRF)
    assert r.status_code == 503  # no gateway configured in tests
