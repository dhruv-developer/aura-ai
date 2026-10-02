from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aura_api.app import create_app
from aura_db.models import Lead

CSRF = {"x-aura-csrf": "1"}
FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "xray_sample.jsonl"


@pytest.fixture
async def api(container):  # type: ignore[no-untyped-def]
    app = create_app(container, sync_catalog_on_startup=False)
    async with app.router.lifespan_context(app):
        yield app


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


async def _login(c, container, email):  # type: ignore[no-untyped-def]
    await c.post("/v1/auth/magic-link", json={"email": email})
    token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
    await c.post("/v1/auth/magic-link/verify", json={"token": token})


async def test_upload_list_get_html_and_isolation(api, container) -> None:  # type: ignore[no-untyped-def]
    async with _client(api) as c:
        await _login(c, container, "cto@acme.io")
        org = (await c.post("/v1/organizations", json={"name": "Acme"}, headers=CSRF)).json()
        acct = org["account_id"]
        r = await c.post(
            f"/v1/accounts/{acct}/xray",
            files={"file": ("traffic.jsonl", FIXTURE.read_bytes(), "application/jsonl")},
            data={"format": "jsonl", "name": "Prod week 38"},
            headers=CSRF,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["record_count"] == 2000 and body["summary"]["est_savings_pct"] > 0
        rid = body["id"]
        listed = (await c.get(f"/v1/accounts/{acct}/xray")).json()
        assert [x["id"] for x in listed] == [rid] and "report" not in listed[0]
        full = (await c.get(f"/v1/accounts/{acct}/xray/{rid}")).json()
        assert full["report"]["opportunities"]
        html = await c.get(f"/v1/accounts/{acct}/xray/{rid}/report.html")
        assert html.status_code == 200 and "AI Workload X-Ray" in html.text
        assert "Prod week 38" in html.text

        # Personal (B2C free) accounts do not include workload analytics.
        me = (await c.get("/v1/me")).json()
        personal = next(a["account_id"] for a in me["accounts"] if a["kind"] == "individual")
        r = await c.post(
            f"/v1/accounts/{personal}/xray",
            files={"file": ("t.jsonl", b"{}", "application/jsonl")},
            headers=CSRF,
        )
        assert r.status_code == 403

    async with _client(api) as eve:
        await _login(eve, container, "eve@evil.io")
        assert (await eve.get(f"/v1/accounts/{acct}/xray/{rid}")).status_code == 404


async def test_public_funnel_records_lead_without_prompts_and_is_rate_limited(
    api, container
) -> None:  # type: ignore[no-untyped-def]
    sample = b"\n".join(FIXTURE.read_bytes().splitlines()[:300])
    async with _client(api) as c:
        r = await c.post(
            "/v1/xray/public",
            data={"email": "Founder@Startup.io", "company": "S"},
            files={"file": ("s.jsonl", sample, "application/jsonl")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["report"]["summary"]["requests"] == 300
        async with container.db.system() as s:
            lead = await s.scalar(select(Lead))
        assert lead.email == "founder@startup.io" and lead.source == "xray_public"
        assert "AcmeBank" not in str(lead.summary)  # aggregates only, never prompt text
        codes = [
            (
                await c.post(
                    "/v1/xray/public",
                    data={"email": "a@b.io"},
                    files={"file": ("s.jsonl", sample, "application/jsonl")},
                )
            ).status_code
            for _ in range(5)
        ]
        assert codes[-1] == 429


async def test_upload_size_limits_are_per_route(api, container) -> None:  # type: ignore[no-untyped-def]
    async with _client(api) as c:
        await _login(c, container, "x@acme.io")
        big = {"name": "x" * 2_000_000}
        r = await c.post("/v1/organizations", json=big, headers=CSRF)
        assert r.status_code == 413  # 1 MB default for ordinary routes
