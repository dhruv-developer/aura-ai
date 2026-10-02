from datetime import UTC, datetime, timedelta

import pytest

from aura_memory import LongTermMemory
from aura_providers.embeddings import HashingEmbedder
from aura_shared.errors import ValidationFailed


async def _acct(container, email="m@acme.io") -> str:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email(email)
    org = await container.identity.create_organization(user.id, "Acme")
    return str(org.account_id)


@pytest.fixture
def ltm(container, redis_client) -> LongTermMemory:  # type: ignore[no-untyped-def]
    return LongTermMemory(container.db, vectors=redis_client, embedder=HashingEmbedder())


FACTS = [
    "The refund window for annual plans is 30 days from purchase.",
    "Support hours are 9am to 6pm IST on weekdays.",
    "Enterprise customers get a dedicated Slack channel.",
    "Invoices are issued on the first business day of each month.",
]


async def test_hybrid_search_ranks_relevant_memory_first(container, ltm) -> None:  # type: ignore[no-untyped-def]
    acct = await _acct(container)
    for f in FACTS:
        await ltm.add(acct, scope="kb", scope_ref="handbook", content=f, kind="document")
    hits = await ltm.search(
        acct, scopes=[("kb", "handbook")], query="how long is the refund window?"
    )
    assert hits and "refund window" in hits[0].content
    hits = await ltm.search(acct, scopes=[("kb", "handbook")], query="when are invoices issued")
    assert "Invoices" in hits[0].content


async def test_dedupe_reinforces_instead_of_duplicating(container, ltm) -> None:  # type: ignore[no-untyped-def]
    acct = await _acct(container)
    a, created_a = await ltm.add(
        acct, scope="user", scope_ref="u1", content="Prefers concise answers in Hindi."
    )
    b, created_b = await ltm.add(
        acct, scope="user", scope_ref="u1", content="prefers concise answers in hindi"
    )
    assert created_a and not created_b and a == b
    [hit] = await ltm.search(
        acct, scopes=[("user", "u1")], query="answer language preference hindi"
    )
    assert hit.content == "prefers concise answers in hindi"  # newest wording wins


async def test_scope_and_tenant_isolation(container, ltm) -> None:  # type: ignore[no-untyped-def]
    a = await _acct(container, "a@acme.io")
    b = await _acct(container, "b@other.io")
    await ltm.add(a, scope="user", scope_ref="u1", content="Secret project codename is Falcon.")
    assert await ltm.search(a, scopes=[("user", "u2")], query="project codename") == []
    assert await ltm.search(b, scopes=[("user", "u1")], query="project codename") == []
    assert (await ltm.search(a, scopes=[("user", "u1")], query="project codename"))[
        0
    ].content.startswith("Secret")


async def test_forget_decay_and_reindex(container, ltm, redis_client) -> None:  # type: ignore[no-untyped-def]
    acct = await _acct(container)
    await ltm.add(acct, scope="user", scope_ref="u1", content="Likes dark mode.", importance=0.1)
    await ltm.add(
        acct, scope="user", scope_ref="u1", content="Works at Acme Bank in Mumbai.", importance=0.9
    )
    archived = await ltm.decay(acct, older_than_days=1, now=datetime.now(UTC) + timedelta(days=5))
    assert archived == 1  # the unused, unimportant memory
    hits = await ltm.search(acct, scopes=[("user", "u1")], query="dark mode")
    assert all("dark mode" not in h.content for h in hits)

    await redis_client.flushdb()  # Redis lost the vector index
    assert await ltm.reindex(acct) == 1
    assert (await ltm.search(acct, scopes=[("user", "u1")], query="where does the user work"))[
        0
    ].content.startswith("Works")

    assert await ltm.forget(acct, scope="user", scope_ref="u1") == 2
    assert await ltm.search(acct, scopes=[("user", "u1")], query="Acme Bank") == []


async def test_full_text_still_works_when_vector_index_is_down(container, ltm, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from redis.exceptions import ConnectionError as RedisConnectionError

    acct = await _acct(container)
    await ltm.add(acct, scope="kb", scope_ref="h", content=FACTS[0])

    async def down(*a, **kw):  # type: ignore[no-untyped-def]
        raise RedisConnectionError("down")

    monkeypatch.setattr(ltm, "_vcmd", down)
    hits = await ltm.search(acct, scopes=[("kb", "h")], query="refund window annual plans")
    assert hits and "refund" in hits[0].content


async def test_validation(container, ltm) -> None:  # type: ignore[no-untyped-def]
    acct = await _acct(container)
    for kw in [
        {"scope": "galaxy", "scope_ref": "x", "content": "c"},
        {"scope": "user", "scope_ref": "", "content": "c"},
        {"scope": "user", "scope_ref": "u", "content": "  "},
        {"scope": "user", "scope_ref": "u", "content": "c", "kind": "dream"},
        {"scope": "user", "scope_ref": "u", "content": "c", "importance": 2},
    ]:
        with pytest.raises(ValidationFailed):
            await ltm.add(acct, **kw)
