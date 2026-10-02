import pytest

from aura_memory import SessionMemory, WorkingMemory
from aura_providers.domain.types import Message
from aura_shared.errors import ValidationFailed


async def test_session_window_respects_budget_order_and_user_first(redis_client) -> None:  # type: ignore[no-untyped-def]
    mem = SessionMemory(redis_client, max_turns=6)
    for i in range(5):
        await mem.append(
            "acct_1", "s1", [Message("user", f"q{i} " + "x" * 40), Message("assistant", f"a{i}")]
        )
    w = await mem.window("acct_1", "s1", token_budget=10_000)
    assert w.turns_total == 6  # capped by max_turns
    assert w.messages[0].role == "user"  # never starts on an assistant turn
    assert w.messages[-1].content == "a4"

    small = await mem.window("acct_1", "s1", token_budget=30)
    assert small.turns_included < w.turns_included and small.est_tokens <= 30
    assert small.messages[-1].content == "a4"  # newest kept first


async def test_summary_tenant_isolation_and_forget(redis_client) -> None:  # type: ignore[no-untyped-def]
    mem = SessionMemory(redis_client)
    await mem.append("acct_a", "s", [Message("user", "secret a")])
    await mem.set_summary("acct_a", "s", "earlier talk")
    w = await mem.window("acct_a", "s", token_budget=1000)
    assert w.messages[0].role == "system" and "earlier talk" in w.messages[0].content
    assert (await mem.window("acct_b", "s", token_budget=1000)).messages == ()
    await mem.forget("acct_a", "s")
    assert (await mem.window("acct_a", "s", token_budget=1000)).messages == ()
    for bad in ["", "a b", "x" * 129, "a:b", "{x}"]:
        with pytest.raises(ValidationFailed):
            await mem.append("acct_a", bad, [Message("user", "x")])


async def test_working_memory(redis_client) -> None:  # type: ignore[no-untyped-def]
    wm = WorkingMemory(redis_client, ttl_s=60)
    await wm.put("acct_1", "run_1", "search", {"hits": 3})
    assert await wm.get("acct_1", "run_1", "search") == {"hits": 3}
    assert await wm.get("acct_2", "run_1", "search") is None
    assert await wm.all("acct_1", "run_1") == {"search": {"hits": 3}}
    assert 0 < await redis_client.ttl(wm._key("acct_1", "run_1")) <= 60
    await wm.clear("acct_1", "run_1")
    assert await wm.all("acct_1", "run_1") == {}
