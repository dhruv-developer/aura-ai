"""Cross-pod invalidation: control-plane change → outbox → relay → every pod's L1."""

import asyncio
import contextlib

from aura_cache import InvalidationBus, L1Cache, RedisL2, TieredCache
from aura_db.models import EntitlementOverride
from aura_shared.ids import new_id
from aura_shared.redis import KeyFamily, RedisScripts
from aura_workers.outbox_relay import OutboxRelay, invalidation_handler


async def _wait_for(predicate) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(2.0):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_outbox_relay_invalidates_other_pods(container, redis_client) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("o@x.io")
    [acc] = await container.identity.list_access(user.id)
    acct = acc.account_id

    # A second "pod": its own L1 and its own invalidation listener on the same Redis.
    pod_b_cache = TieredCache(
        KeyFamily.ENTITLEMENTS,
        l1=L1Cache(100),
        l2=RedisL2(redis_client),
        scripts=RedisScripts(redis_client),
        l1_ttl_s=3600,
    )
    bus_b = InvalidationBus(redis_client, env="test")
    bus_b.register([pod_b_cache])
    await bus_b.start()
    try:
        loads = 0

        async def load() -> int:
            nonlocal loads
            loads += 1
            return loads

        assert await pod_b_cache.get_or_load(acct, "set", loader=load) == 1
        assert await pod_b_cache.get_or_load(acct, "set", loader=load) == 1  # L1 hit

        # Control plane: change entitlements, enqueue the outbox row in the same txn.
        async with container.db.tenant(acct) as s:
            s.add(
                EntitlementOverride(
                    id=new_id("ovr"),
                    account_id=acct,
                    feature_key="executions",
                    enabled=True,
                    limit=9,
                    period="month",
                    reason="t",
                    created_by="t",
                )
            )
            await container.entitlements.enqueue_invalidation(s, acct)
        await redis_client.delete(pod_b_cache.key(acct, "set"))  # L2 dropped by the writer

        relay = OutboxRelay(container.db, invalidation_handler(container.invalidation.publish))
        assert await relay.relay_once() == 1
        assert await relay.relay_once() == 0  # marked published
        await _wait_for(lambda: bus_b.received >= 1)
        assert await pod_b_cache.get_or_load(acct, "set", loader=load) == 2  # reloaded
    finally:
        await bus_b.close()


async def test_relay_handler_failure_leaves_rows_for_retry(container) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("p@x.io")
    [acc] = await container.identity.list_access(user.id)
    async with container.db.tenant(acc.account_id) as s:
        await container.entitlements.enqueue_invalidation(s, acc.account_id)

    async def broken(topic, key, payload):  # type: ignore[no-untyped-def]
        raise RuntimeError("bus down")

    with contextlib.suppress(RuntimeError):
        await OutboxRelay(container.db, broken).relay_once()
    seen: list[str] = []

    async def ok(topic, key, payload):  # type: ignore[no-untyped-def]
        seen.append(key)

    assert await OutboxRelay(container.db, ok).relay_once() == 1 and len(seen) == 1
