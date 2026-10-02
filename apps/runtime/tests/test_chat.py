"""The app's chat: saved conversations, streaming, sub-queries (legacy path behind the
``chat_agents`` kill switch) and the multi-agent Drafter ↔ Reviewer → Finaliser path."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select

from aura_db.models import Conversation, ConversationMessage
from aura_runtime.app import create_app

CSRF = {"x-aura-csrf": "1"}


@pytest.fixture
async def rt(runtime) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt.test") as c,
    ):
        yield c


@pytest.fixture
async def rt_legacy(runtime) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    """The pre-agents answer paths (kill switch off)."""
    runtime.settings = runtime.settings.model_copy(update={"chat_agents": False})
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rt.test") as c,
    ):
        yield c


async def _sign_in(container, rt: httpx.AsyncClient, email: str) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    rt.cookies.set(container.settings.session_cookie_name, await container.sessions.create(user.id))


def _events(body: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _send(rt: httpx.AsyncClient, conv: str, content: str, mode: str | None = None):  # type: ignore[no-untyped-def]
    body = {"content": content} | ({"mode": mode} if mode else {})
    r = await rt.post(f"/v1/conversations/{conv}/messages", json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    return _events(r.text)


async def test_conversation_lifecycle_and_history(rt_legacy, container) -> None:  # type: ignore[no-untyped-def]
    rt = rt_legacy
    await _sign_in(container, rt, "chat@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    assert conv["title"] == "New chat" and conv["mode"] == "auto"

    events = await _send(rt, conv["id"], "Explain photosynthesis simply")
    kinds = [k for k, _ in events]
    assert kinds[0] == "user" and kinds[-1] == "done" and "delta" in kinds
    streamed = "".join(d["text"] for k, d in events if k == "delta")
    done = events[-1][1]
    assert done["content"] == streamed and done["aura"]["kind"] == "single"
    assert done["aura"]["model"] and "baseline_cost_usd" in done["aura"]

    await _send(rt, conv["id"], "Now in one sentence", mode="fast")
    full = (await rt.get(f"/v1/conversations/{conv['id']}")).json()
    assert full["title"] == "Explain photosynthesis simply"  # titled from the first message
    assert full["mode"] == "fast"
    assert [m["role"] for m in full["messages"]] == ["user", "assistant", "user", "assistant"]

    second = (await rt.post("/v1/conversations", json={"title": "Trip"}, headers=CSRF)).json()
    listed = (await rt.get("/v1/conversations")).json()["data"]
    assert [c["id"] for c in listed] == [second["id"], conv["id"]]  # most recent first
    r = await rt.patch(f"/v1/conversations/{conv['id']}", json={"title": "Plants"}, headers=CSRF)
    assert r.json()["title"] == "Plants"
    assert (await rt.delete(f"/v1/conversations/{second['id']}", headers=CSRF)).status_code == 204
    assert len((await rt.get("/v1/conversations")).json()["data"]) == 1


async def test_titles_and_messages_are_encrypted_at_rest(rt, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt, "secret@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    await _send(rt, conv["id"], "my diagnosis is hypothyroidism")
    async with container.db.system() as s:
        blobs = [c.ciphertext for c in await s.scalars(select(Conversation))]
        blobs += [m.ciphertext for m in await s.scalars(select(ConversationMessage))]
    assert blobs and all(b"hypothyroidism" not in b for b in blobs)


async def test_conversations_are_private_per_user(rt, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt, "alice@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    rt.cookies.clear()
    await _sign_in(container, rt, "bob@x.io")
    assert (await rt.get(f"/v1/conversations/{conv['id']}")).status_code == 404
    r = await rt.post(
        f"/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=CSRF
    )
    assert r.status_code == 404
    assert (await rt.get("/v1/conversations")).json()["data"] == []


async def test_auto_mode_splits_multi_part_questions(rt_legacy, container) -> None:  # type: ignore[no-untyped-def]
    rt = rt_legacy
    await _sign_in(container, rt, "multi@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    question = "What is retrieval augmented generation? How does it reduce hallucinations?"
    events = await _send(rt, conv["id"], question)
    plan = next(d for k, d in events if k == "plan")
    assert plan["subqueries"] == [
        "What is retrieval augmented generation?",
        "How does it reduce hallucinations?",
    ]
    done = events[-1][1]
    aura = done["aura"]
    assert aura["kind"] == "subqueries" and len(aura["subqueries"]) == 2
    assert all(p["model"] for p in aura["subqueries"]) and aura["combine_model"]
    assert done["content"]

    # Only Auto decomposes: an explicit mode answers in one call.
    events = await _send(rt, conv["id"], question, mode="best")
    assert "plan" not in [k for k, _ in events]
    assert events[-1][1]["aura"]["kind"] == "single"


async def test_api_keys_cannot_use_conversations(rt, container) -> None:  # type: ignore[no-untyped-def]
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
    r = await rt.get("/v1/conversations", headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 403


async def test_explicit_model_and_model_list(rt_legacy, container) -> None:  # type: ignore[no-untyped-def]
    rt = rt_legacy
    await _sign_in(container, rt, "picker@x.io")
    models = (await rt.get("/v1/models")).json()["data"]
    stub = {m["id"]: m["aura"] for m in models if m["id"].startswith("stub/")}
    assert stub["stub/medium"]["usable"] and stub["stub/medium"]["display_name"]
    assert not any(m["aura"]["usable"] for m in models if m["id"].startswith("anthropic/"))

    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    multi = "What is retrieval augmented generation? How does it reduce hallucinations?"
    r = await rt.post(
        f"/v1/conversations/{conv['id']}/messages",
        json={"content": multi, "model": "stub/frontier"},
        headers=CSRF,
    )
    events = _events(r.text)
    assert "plan" not in [k for k, _ in events]  # a chosen model means no split
    assert events[-1][1]["aura"]["model"] == "stub/frontier"
    bad = await rt.post(
        f"/v1/conversations/{conv['id']}/messages",
        json={"content": "hi", "model": "nope/nothing"},
        headers=CSRF,
    )
    assert bad.status_code == 422
    history = (await rt.get(f"/v1/conversations/{conv['id']}")).json()["messages"]
    assert len(history) == 2  # the rejected message was never stored


async def test_answer_survives_the_listener_leaving() -> None:
    import asyncio

    from aura_runtime.chat import _RUNNING, _detach

    saved: list[str] = []
    release = asyncio.Event()

    async def producer():  # type: ignore[no-untyped-def]
        yield "event: user\ndata: {}\n\n"
        await release.wait()  # the slow model
        saved.append("answer stored")
        yield "event: done\ndata: {}\n\n"

    stream = _detach("conv_x", producer())
    assert await anext(stream) == "event: user\ndata: {}\n\n"
    await stream.aclose()  # the browser navigates away
    assert "conv_x" in _RUNNING and not _RUNNING["conv_x"].done()
    release.set()
    await asyncio.wait_for(_RUNNING["conv_x"], 2)
    assert saved == ["answer stored"] and "conv_x" not in _RUNNING


async def test_stop_and_status_endpoints(rt, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt, "stopper@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    assert (await rt.get(f"/v1/conversations/{conv['id']}/status")).json() == {"answering": False}
    r = await rt.post(f"/v1/conversations/{conv['id']}/stop", headers=CSRF)
    assert r.json() == {"stopped": False}
    rt.cookies.clear()
    await _sign_in(container, rt, "intruder@x.io")
    assert (await rt.post(f"/v1/conversations/{conv['id']}/stop", headers=CSRF)).status_code == 404


# ---- multi-agent path (the default) ----------------------------------------------------


async def test_every_message_is_drafted_reviewed_and_finalised(rt, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt, "agents@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()

    events = await _send(rt, conv["id"], "Explain photosynthesis simply")
    agent = [d["type"] for k, d in events if k == "agent"]
    assert agent[:2] == ["splitting", "split"]
    assert "draft" in agent and "review" in agent and "node_done" in agent
    assert agent.index("draft") < agent.index("review") < agent.index("finalise")
    done = events[-1][1]
    assert done["aura"]["kind"] == "agents" and done["aura"]["nodes"] == 1
    assert done["aura"]["drafts"] >= 1 and done["aura"]["reviews"] >= 1
    tree = done["trace"]["tree"]
    assert tree[0]["question"] == "Explain photosynthesis simply"
    assert [t["role"] for t in tree[0]["turns"]][:2] == ["drafter", "reviewer"]

    multi = "What is retrieval augmented generation? How does it reduce hallucinations?"
    events = await _send(rt, conv["id"], multi)
    split = next(d for k, d in events if k == "agent" and d["type"] == "split")
    assert [n["question"] for n in split["nodes"]] == [
        "What is retrieval augmented generation?",
        "How does it reduce hallucinations?",
    ]
    assert any(d["type"] == "finalised" for k, d in events if k == "agent")  # 2 parts combined
    assert events[-1][1]["aura"]["nodes"] == 2

    reopened = (await rt.get(f"/v1/conversations/{conv['id']}")).json()["messages"]
    assert reopened[-1]["trace"]["tree"][1]["question"] == "How does it reduce hallucinations?"


async def test_agent_tree_is_encrypted_and_aura_is_content_free(rt, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt, "private@x.io")
    conv = (await rt.post("/v1/conversations", json={}, headers=CSRF)).json()
    secret = "What is my zirconium allergy? How do I treat a zirconium rash?"
    events = await _send(rt, conv["id"], secret)
    aura = events[-1][1]["aura"]
    assert "zirconium" not in json.dumps(aura)
    async with container.db.system() as s:
        rows = list(await s.scalars(select(ConversationMessage)))
    assert any(r.trace_ciphertext for r in rows)
    for r in rows:
        assert b"zirconium" not in r.ciphertext
        assert b"zirconium" not in (r.trace_ciphertext or b"")
        assert "zirconium" not in json.dumps(r.aura or {})


async def test_kill_switch_restores_single_answers(rt_legacy, container) -> None:  # type: ignore[no-untyped-def]
    await _sign_in(container, rt_legacy, "legacy@x.io")
    conv = (await rt_legacy.post("/v1/conversations", json={}, headers=CSRF)).json()
    events = await _send(rt_legacy, conv["id"], "Explain photosynthesis simply")
    assert "agent" not in [k for k, _ in events]
    assert events[-1][1]["aura"]["kind"] == "single"
    multi = "What is retrieval augmented generation? How does it reduce hallucinations?"
    done = (await _send(rt_legacy, conv["id"], multi))[-1][1]
    assert all("question" not in p for p in done["aura"]["subqueries"])  # no plaintext leak
    assert done["trace"]["subqueries"][0] == "What is retrieval augmented generation?"
