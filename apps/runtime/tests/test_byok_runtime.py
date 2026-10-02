"""BYOK end to end: a tenant's own Anthropic key is used for that tenant only."""

import json

import anthropic
import httpx
import httpx2

from aura_engine import InMemoryPublisher
from aura_providers.adapters.anthropic_provider import AnthropicProvider
from aura_runtime.app import create_app
from aura_runtime.container import RuntimeContainer

TENANT_KEY = "sk-ant-tenant-0123456789abcdef"
CSRF = {"x-aura-csrf": "1"}


def _anthropic_mock(seen: list[httpx2.Request]) -> AnthropicProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "from claude"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicProvider(http_client=http, max_retries=0)


async def _org_with_key(container, email: str):  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    org = await container.identity.create_organization(user.id, "Acme")
    access = await container.identity.resolve_access(user.id, org.account_id)
    _, key = await container.api_keys.issue(
        account_id=org.account_id,
        actor_user_id=user.id,
        actor_permissions=access.permissions,
        name="rt",
        permissions={"runtime:execute"},
        environment_id=org.environment_ids["production"],
    )
    return user, org, key


async def test_byok_routes_through_tenant_key_only(container) -> None:  # type: ignore[no-untyped-def]
    seen: list[httpx2.Request] = []
    settings = container.settings.model_copy(update={"stub_provider": True})
    rt = RuntimeContainer.build(
        settings,
        publisher=InMemoryPublisher(),
        provider_overrides={"anthropic": _anthropic_mock(seen)},
    )
    app = create_app(rt)
    try:
        async with app.router.lifespan_context(app):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt")
            _owner, org, rt_key = await _org_with_key(container, "byok@acme.io")
            _, _, other_key = await _org_with_key(container, "nobyok@other.io")

            # Add the tenant key through the control-plane API (entitlement-gated).
            from aura_api.app import create_app as create_api

            api = create_api(container, sync_catalog_on_startup=False)
            async with (
                api.router.lifespan_context(api),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api), base_url="http://api"
                ) as cp,
            ):
                await cp.post("/v1/auth/magic-link", json={"email": "byok@acme.io"})
                token = container.magic_links._mailer.outbox[-1].text.split("token=")[1].split()[0]
                await cp.post("/v1/auth/magic-link/verify", json={"token": token})
                r = await cp.post(
                    f"/v1/accounts/{org.account_id}/provider-credentials",
                    json={"provider": "anthropic", "api_key": TENANT_KEY},
                    headers=CSRF,
                )
                assert r.status_code == 201 and "api_key" not in r.json()
                assert r.json()["last4"] == "cdef"

            body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
            r = await client.post(
                "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {rt_key}"}
            )
            assert r.status_code == 200, r.text
            assert r.json()["choices"][0]["message"]["content"] == "from claude"
            assert r.json()["aura"]["credential_source"] == "byok"
            assert seen[-1].headers["x-api-key"] == TENANT_KEY

            # Another tenant without a key cannot use Anthropic at all (no platform key).
            r = await client.post(
                "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {other_key}"}
            )
            assert r.status_code == 403
            assert len(seen) == 1
            await client.aclose()
    finally:
        await rt.close()
