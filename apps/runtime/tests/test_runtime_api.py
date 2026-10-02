"""HTTP tests for the data plane: OpenAI-compatible + native endpoints."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from aura_providers.domain.types import ProviderErrorKind
from aura_runtime.app import create_app


@pytest.fixture
async def rt_client(runtime) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt.test") as c,
    ):
        yield c


async def _org_key(container, permissions=None) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("dev@acme.io")
    org = await container.identity.create_organization(user.id, "Acme")
    access = await container.identity.resolve_access(user.id, org.account_id)
    _, key = await container.api_keys.issue(
        account_id=org.account_id,
        actor_user_id=user.id,
        actor_permissions=access.permissions,
        name="rt",
        permissions=permissions or {"runtime:execute"},
        environment_id=org.environment_ids["production"],
    )
    return org.account_id, key


def _auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def test_chat_completion_openai_shape(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    r = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "aura/auto", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion" and body["model"] == "stub/small"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "[small] hello"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0 and float(body["aura"]["cost_usd"]) > 0
    assert r.headers["x-aura-model"] == "stub/small" and r.headers["x-aura-trace-id"].startswith(
        "trc_"
    )


async def test_aliases_explicit_models_and_aura_options(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    msgs = [
        {"role": "developer", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "x"}]},
    ]
    best = await rt_client.post(
        "/v1/chat/completions", headers=_auth(key), json={"model": "aura/best", "messages": msgs}
    )
    assert best.json()["model"] == "stub/frontier"
    explicit = await rt_client.post(
        "/v1/chat/completions", headers=_auth(key), json={"model": "stub/medium", "messages": msgs}
    )
    assert explicit.json()["model"] == "stub/medium"
    denied = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "stub/medium", "messages": msgs, "aura": {"allowed_models": ["stub/small"]}},
    )
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    unknown = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "gpt-imaginary", "messages": msgs},
    )
    assert unknown.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [{"tools": [{"type": "function"}]}, {"n": 2}, {"response_format": {"type": "json_object"}}],
)
async def test_unsupported_features_rejected_not_silently_dropped(
    rt_client, container, extra
) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    r = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "x"}], **extra},
    )
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")


async def test_streaming_sse(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    async with rt_client.stream(
        "POST",
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "one two three"}], "stream": True},
    ) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        lines = [ln async for ln in r.aiter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "[small] one two three"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop" and "aura" in chunks[-1]


async def test_stream_that_cannot_start_returns_http_error(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    for m in ("small", "medium", "frontier"):
        runtime.stub.fail_next(m, ProviderErrorKind.UNAVAILABLE)
    r = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "x"}], "stream": True},
    )
    assert r.status_code == 503 and r.json()["code"] == "dependency_unavailable"


async def test_execute_endpoint(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    r = await rt_client.post(
        "/v1/execute",
        headers=_auth(key),
        json={
            "goal": "Explain this paper",
            "context": "abstract...",
            "constraints": {"quality": 0.97, "max_cost": 0.5},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "aura.execution" and body["model"] == "stub/frontier"
    assert body["output"].startswith("[frontier] Explain this paper")


async def test_auth_permission_entitlement_and_rate_limit(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    msgs = {"messages": [{"role": "user", "content": "x"}]}
    assert (await rt_client.post("/v1/chat/completions", json=msgs)).status_code == 401
    bad = await rt_client.post(
        "/v1/chat/completions", headers=_auth("aura_sk_prod_aaaaaaaaaaaa_" + "x" * 43), json=msgs
    )
    assert bad.status_code == 401

    _, telemetry_only = await _org_key(container, {"telemetry:write"})
    assert (
        await rt_client.post("/v1/chat/completions", headers=_auth(telemetry_only), json=msgs)
    ).status_code == 403

    # Free personal plan has no api_access entitlement.
    user = await container.identity.login_with_email("free@x.io")
    [acc] = await container.identity.list_access(user.id)
    _, free_key = await container.api_keys.issue(
        account_id=acc.account_id,
        actor_user_id=user.id,
        actor_permissions=acc.permissions,
        name="k",
        permissions={"runtime:execute"},
        environment_id=None,
    )
    r = await rt_client.post("/v1/chat/completions", headers=_auth(free_key), json=msgs)
    assert r.status_code == 403 and r.json()["feature"] == "api_access"

    runtime.settings = runtime.settings.model_copy(
        update={"runtime_rate_per_key_per_s": 0.01, "runtime_burst_per_key": 2}
    )
    _, key = await _org_key(container)
    codes = [
        (await rt_client.post("/v1/chat/completions", headers=_auth(key), json=msgs)).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [200, 200] and codes[2] == 429


async def test_models_and_body_limit(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    ids = [m["id"] for m in (await rt_client.get("/v1/models", headers=_auth(key))).json()["data"]]
    assert (
        ids[:4] == ["aura/auto", "aura/fast", "aura/balanced", "aura/best"] and "stub/small" in ids
    )
    huge = "x" * (runtime.settings.runtime_max_request_bytes + 1)
    r = await rt_client.post("/v1/chat/completions", headers=_auth(key), content=huge)
    assert r.status_code == 413


async def test_workload_policy_and_outcomes_over_http(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    acct, key = await _org_key(container)
    user = await container.identity.login_with_email("dev@acme.io")
    created = await container.policies.create_policy(
        account_id=acct,
        name="support",
        scope="workload",
        scope_ref="support",
        spec={"allowed_models": ["stub/medium"]},
        actor_id=user.id,
    )
    await container.policies.deploy(
        account_id=acct,
        policy_id=created["policy_id"],
        version_id=created["version_id"],
        strategy="immediate",
        actor_id=user.id,
        force_reason="authored",
    )
    body = {"messages": [{"role": "user", "content": "x"}], "aura": {"workload": "support"}}
    r = await rt_client.post("/v1/chat/completions", headers=_auth(key), json=body)
    assert r.status_code == 200 and r.json()["model"] == "stub/medium"
    execution_id = r.json()["aura"]["execution_id"]
    plain = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert plain.json()["model"] == "stub/small"  # no workload → no workload policy

    ok = await rt_client.post(
        "/v1/outcomes",
        headers=_auth(key),
        json={"execution_id": execution_id, "outcome": "success"},
    )
    assert ok.status_code == 202
    missing = await rt_client.post(
        "/v1/outcomes",
        headers=_auth(key),
        json={"execution_id": "exe_unknown", "outcome": "success"},
    )
    assert missing.status_code == 404


async def test_execute_context_compilation_modes(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    filler = "\n\n".join(
        f"Section {i}. Facilities report on parking, cafeteria menus and elevator schedules."
        for i in range(400)
    )
    needle = "Refund policy: refunds are allowed within 30 days of purchase."
    context = filler + "\n\n" + needle
    base = {
        "goal": "How many days do I have to request a refund?",
        "context": context,
        "max_tokens": 100_000,
    }

    strict = await rt_client.post("/v1/execute", headers=_auth(key), json=base)
    assert strict.status_code == 200, strict.text
    assert not any("context compiled" in n for n in strict.json()["aura"]["notes"])
    assert strict.json()["usage"]["prompt_tokens"] > 5_000  # everything was sent

    compiled = await rt_client.post(
        "/v1/execute",
        headers=_auth(key),
        json=base | {"constraints": {"context_mode": "auto", "context_budget": 512}},
    )
    body = compiled.json()
    assert compiled.status_code == 200, compiled.text
    assert any("context compiled" in n for n in body["aura"]["notes"])
    assert body["usage"]["prompt_tokens"] < 1_000
    assert "30 days" in body["output"]  # the stub echoes its prompt: the fact survived


async def test_memory_knowledge_rag_and_tuning_over_http(rt_client, container) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    handbook = "\n\n".join(
        [
            "Refunds: annual plans can be refunded within 30 days of purchase.",
            "Support: our team answers from 9am to 6pm IST on weekdays.",
            "Security: all data is encrypted at rest with AES-256.",
            *[f"Section {i}: office facilities, parking and cafeteria notes." for i in range(30)],
        ]
    )
    r = await rt_client.post(
        "/v1/knowledge/handbook/documents",
        headers=_auth(key),
        json={"text": handbook, "source": "handbook.md"},
    )
    assert r.status_code == 201 and r.json()["chunks"] >= 3
    await rt_client.post(
        "/v1/memories",
        headers=_auth(key),
        json={
            "scope": "user",
            "scope_ref": "u1",
            "content": "Prefers answers in Hindi.",
            "kind": "preference",
        },
    )

    s = await rt_client.get(
        "/v1/memories/search",
        headers=_auth(key),
        params={"scope": "kb", "scope_ref": "handbook", "q": "refund window"},
    )
    assert "30 days" in s.json()["data"][0]["content"]

    chat = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "messages": [{"role": "user", "content": "How long do I have to get a refund?"}],
            "aura": {"memory": ["kb:handbook", "user:u1"]},
        },
    )
    body = chat.json()
    assert chat.status_code == 200, chat.text
    assert any(n.startswith("memory: ") and "injected" in n for n in body["aura"]["notes"])

    tuned = await rt_client.post(
        "/v1/knowledge/handbook/tune",
        headers=_auth(key),
        json={
            "questions": [
                {"question": "refund period for annual plans", "expected": "30 days"},
                {"question": "support hours", "expected": "9am to 6pm"},
                {"question": "is data encrypted", "expected": "AES-256"},
            ]
        },
    )
    rec = tuned.json()["recommendation"]
    assert rec["meets_target"] and rec["recall"] >= 0.9 and rec["token_budget"] == 500

    bad = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "x"}], "aura": {"memory": ["galaxy:x"]}},
    )
    assert bad.status_code == 422
    forgot = await rt_client.delete(
        "/v1/memories", headers=_auth(key), params={"scope": "user", "scope_ref": "u1"}
    )
    assert forgot.json()["deleted"] == 1


async def test_registry_prompt_used_by_runtime(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    acct, key = await _org_key(container)
    user = await container.identity.login_with_email("dev@acme.io")
    ids = await container.prompts.create(
        acct, name="greeter", content="Greet as {{brand}}.", actor_id=user.id
    )
    r = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "aura": {"prompt": "greeter", "prompt_vars": {"brand": "Acme"}},
        },
    )
    assert r.status_code == 200, r.text
    assert list(runtime.publisher.events)[-1].prompt_version_id == ids["version_id"]
    missing_var = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "hello"}], "aura": {"prompt": "greeter"}},
    )
    assert missing_var.status_code == 422
    unknown = await rt_client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"messages": [{"role": "user", "content": "hello"}], "aura": {"prompt": "nope"}},
    )
    assert unknown.status_code == 404


async def test_planned_execution_and_async_runs(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    from aura_db.jobs import JobQueue
    from aura_graph.service import JOB_KIND
    from aura_workers.jobs_runner import JobRunner

    acct, key = await _org_key(container)
    math = await rt_client.post(
        "/v1/execute", headers=_auth(key), json={"goal": "what is 6 * 7?", "mode": "planned"}
    )
    body = math.json()
    assert math.status_code == 200, math.text
    assert body["output"] == "42" and body["aura"]["intent"]["task"] == "math"
    assert body["aura"]["trace"]["cost_usd"] == "0"  # no model needed
    assert runtime.stub.calls == []

    await rt_client.post(
        "/v1/knowledge/handbook/documents",
        headers=_auth(key),
        json={"text": "Refunds: annual plans are refundable within 30 days."},
    )
    qa = await rt_client.post(
        "/v1/execute",
        headers=_auth(key),
        json={
            "goal": "what is the refund window for annual plans?",
            "mode": "planned",
            "memory": ["kb:handbook"],
        },
    )
    qa_body = qa.json()
    assert qa_body["aura"]["graph"]["template"] == "qa"
    assert [n["id"] for n in qa_body["aura"]["trace"]["nodes"]][:2] == ["retrieve", "answer"]
    assert "30 days" in qa_body["output"]

    # Async runs need long_running_workflows: the Startup plan does not include it.
    denied = await rt_client.post(
        "/v1/runs", headers=_auth(key), json={"goal": "Research EV charging competitors"}
    )
    assert denied.status_code == 403
    await container.subscriptions.change_plan(
        account_id=acct, plan_code="growth", actor_type="system", actor_id="t", reason="t"
    )
    submitted = await rt_client.post(
        "/v1/runs", headers=_auth(key), json={"goal": "Research EV charging competitors"}
    )
    assert submitted.status_code == 202
    run_id = submitted.json()["id"]
    assert (await rt_client.get(f"/v1/runs/{run_id}", headers=_auth(key))).json()[
        "status"
    ] == "queued"
    assert await JobRunner(JobQueue(container.db), {JOB_KIND: runtime.graphs.process}).run_once()
    done = (await rt_client.get(f"/v1/runs/{run_id}", headers=_auth(key))).json()
    assert done["status"] == "completed" and done["template"] == "research"
    assert done["output"] and float(done["cost_usd"]) > 0

    _, other_key = await _org_key(container)
    assert (await rt_client.get(f"/v1/runs/{run_id}", headers=_auth(other_key))).status_code == 404


async def test_pii_policy_redacts_before_the_provider(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    _, key = await _org_key(container)
    body = {
        "model": "aura/auto",
        "messages": [{"role": "user", "content": "email me at priya@example.com"}],
    }
    r = await rt_client.post(
        "/v1/chat/completions", headers=_auth(key), json={**body, "aura": {"pii": "redact"}}
    )
    assert r.status_code == 200, r.text
    # The stub echoes what it received: the provider never saw the address.
    assert (
        "priya@example.com" not in r.text
        and "[EMAIL]" in r.json()["choices"][0]["message"]["content"]
    )
    blocked = await rt_client.post(
        "/v1/chat/completions", headers=_auth(key), json={**body, "aura": {"pii": "block"}}
    )
    assert blocked.status_code == 422 and blocked.json()["code"] == "pii_blocked"
    assert "priya" not in blocked.text


async def test_human_approval_gates_the_output(rt_client, container, runtime) -> None:  # type: ignore[no-untyped-def]
    from aura_db.jobs import JobQueue
    from aura_graph.service import JOB_KIND
    from aura_workers.jobs_runner import JobRunner

    acct, key = await _org_key(container)
    await container.subscriptions.change_plan(
        account_id=acct, plan_code="growth", actor_type="system", actor_id="t", reason="t"
    )
    user = await container.identity.login_with_email("dev@acme.io")
    session = {
        container.settings.session_cookie_name: await container.sessions.create(user.id),
    }
    reviewer = {"x-aura-csrf": "1", "x-aura-account": acct}
    jobs = JobRunner(JobQueue(container.db), {JOB_KIND: runtime.graphs.process})

    async def submit() -> str:
        r = await rt_client.post(
            "/v1/runs",
            headers=_auth(key),
            json={"goal": "Draft a reply to the customer", "require_approval": True},
        )
        assert r.status_code == 202, r.text
        assert await jobs.run_once()
        return str(r.json()["id"])

    run_id = await submit()
    paused = (await rt_client.get(f"/v1/runs/{run_id}", headers=_auth(key))).json()
    assert paused["status"] == "awaiting_approval"
    draft = paused["pending_approval"]["approval"]
    assert draft

    # Machines cannot approve; people can, once.
    denied = await rt_client.post(
        f"/v1/runs/{run_id}/approval", headers=_auth(key), json={"decision": "approved"}
    )
    assert denied.status_code == 403
    rt_client.cookies.update(session)  # bearer requests still win over the cookie
    ok = await rt_client.post(
        f"/v1/runs/{run_id}/approval",
        headers=reviewer,
        json={"decision": "approved", "reason": "looks right"},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "queued", ok.text
    again = await rt_client.post(
        f"/v1/runs/{run_id}/approval",
        headers=reviewer,
        json={"decision": "approved"},
    )
    assert again.status_code == 409
    assert await jobs.run_once()
    done = (await rt_client.get(f"/v1/runs/{run_id}", headers=_auth(key))).json()
    assert done["status"] == "completed" and done["output"] == draft

    rejected_id = await submit()
    r = await rt_client.post(
        f"/v1/runs/{rejected_id}/approval",
        headers=reviewer,
        json={"decision": "rejected", "reason": "wrong tone"},
    )
    assert r.json()["status"] == "rejected"
    assert not await jobs.run_once()  # nothing resumes
