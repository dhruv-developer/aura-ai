"""``aura-workers`` entrypoint: ``telemetry`` | ``migrate-clickhouse``."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import httpx

from aura_billing.application.entitlements import EntitlementService
from aura_billing.application.usage import UsageReconciler
from aura_billing.domain.catalog import load_catalog
from aura_cache import InvalidationBus, L1Cache, RedisL2, TieredCache
from aura_db import Database
from aura_shared.redis import KeyFamily, RedisPools, RedisRole, RedisScripts
from aura_shared.settings import get_settings
from aura_workers import nats_runner
from aura_workers.outbox_relay import OutboxRelay, invalidation_handler
from aura_workers.sinks import ClickHouseSink
from aura_workers.telemetry import TelemetryProcessor

DDL_DIR = Path(__file__).resolve().parents[4] / "infrastructure" / "clickhouse"


async def _telemetry() -> None:
    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    state = pools.get(RedisRole.STATE)
    scripts = RedisScripts(state)
    catalog = load_catalog()
    ent = EntitlementService(
        db,
        cache=TieredCache(
            KeyFamily.ENTITLEMENTS,
            l1=L1Cache(10_000),
            l2=RedisL2(pools.get(RedisRole.CACHE)),
            scripts=scripts,
        ),
        scripts=scripts,
        catalog=catalog,
    )
    reconciler = UsageReconciler(db, scripts=scripts, entitlements=ent, catalog=catalog)
    async with httpx.AsyncClient() as http:
        processor = TelemetryProcessor(
            sink=ClickHouseSink(http, s.clickhouse_url),
            state=state,
            reconcile=reconciler.flush,
        )
        await nats_runner.run(s.nats_url, processor)


async def _jobs() -> None:
    import httpx as _httpx

    from aura_db.jobs import JobQueue
    from aura_engine.wiring import build_provider_stack
    from aura_shadow import ShadowRunner
    from aura_shadow.service import JOB_KIND as SHADOW
    from aura_workers.jobs_runner import JobRunner

    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
    scripts = RedisScripts(state)

    def tiered(family: KeyFamily) -> TieredCache:
        return TieredCache(family, l1=L1Cache(10_000), l2=RedisL2(cache_redis), scripts=scripts)

    ent = EntitlementService(
        db, cache=tiered(KeyFamily.ENTITLEMENTS), scripts=scripts, catalog=load_catalog()
    )
    async with _httpx.AsyncClient() as http:
        stack = build_provider_stack(
            s,
            db=db,
            http=http,
            entitlements=ent,
            byok_cache=tiered(KeyFamily.PROVIDER_CREDENTIAL),
        )
        runner = ShadowRunner(
            db, envelope=stack.envelope, catalog=stack.catalog, registry=stack.registry
        )
        from aura_prompts import PromptOptimizationRunner, PromptRegistry
        from aura_prompts.optimizer import JOB_KIND as PROMPT_OPT

        prompt_runner = PromptOptimizationRunner(
            db,
            envelope=stack.envelope,
            catalog=stack.catalog,
            registry=stack.registry,
            prompts=PromptRegistry(db, cache=tiered(KeyFamily.PROMPT), entitlements=ent),
        )
        from aura_billing.application.consumer import CreditGate, CreditMeter
        from aura_billing.application.credits import CreditService
        from aura_graph.service import JOB_KIND as GRAPH_RUN
        from aura_graph.service import GraphRunService
        from aura_graph.wiring import build_executor
        from aura_memory import LongTermMemory, WorkingMemory
        from aura_providers.embeddings import HashingEmbedder

        memory = LongTermMemory(db, vectors=pools.get(RedisRole.VECTOR), embedder=HashingEmbedder())
        credits = CreditService(db)
        gate = CreditGate(credits, ent, tiered(KeyFamily.CREDIT_BALANCE))
        executor, can_search = build_executor(
            s,
            catalog=stack.catalog,
            registry=stack.registry,
            http=http,
            memory=memory,
            checkpoints=WorkingMemory(state),
            credit_gate=gate,
            credit_meter=CreditMeter(credits, gate),
        )
        graphs = GraphRunService(
            db, executor=executor, entitlements=ent, envelope=stack.envelope, can_search=can_search
        )
        await JobRunner(
            JobQueue(db),
            {SHADOW: runner.process, PROMPT_OPT: prompt_runner.process, GRAPH_RUN: graphs.process},
        ).run_forever()


async def _canary_monitor() -> None:
    from aura_deploy import CanaryMonitor, PolicyService, VariantStats

    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
    scripts = RedisScripts(state)
    bus = InvalidationBus(cache_redis, env=s.env.value)

    def tiered(family: KeyFamily) -> TieredCache:
        return TieredCache(
            family,
            l1=L1Cache(1_000),
            l2=RedisL2(cache_redis),
            scripts=scripts,
            publisher=bus.publish,
        )

    ent = EntitlementService(
        db, cache=tiered(KeyFamily.ENTITLEMENTS), scripts=scripts, catalog=load_catalog()
    )
    monitor = CanaryMonitor(
        db,
        policies=PolicyService(db, entitlements=ent, resolver_cache=tiered(KeyFamily.POLICY)),
        stats=VariantStats(state),
    )
    while True:
        for action in await monitor.run_once():
            if action["decision"] != "wait":
                logging.getLogger("aura.workers.canary").info("canary %s", action)
        await asyncio.sleep(60)


async def _autopilot() -> None:
    from sqlalchemy import select

    from aura_db.models import Account
    from aura_deploy import AutopilotService, PolicyService
    from aura_engine.learning import LearnedSelector, aggregate_prior

    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
    scripts = RedisScripts(state)
    bus = InvalidationBus(cache_redis, env=s.env.value)

    def tiered(family: KeyFamily) -> TieredCache:
        return TieredCache(
            family,
            l1=L1Cache(1_000),
            l2=RedisL2(cache_redis),
            scripts=scripts,
            publisher=bus.publish,
        )

    ent = EntitlementService(
        db, cache=tiered(KeyFamily.ENTITLEMENTS), scripts=scripts, catalog=load_catalog()
    )
    autopilot = AutopilotService(
        db, policies=PolicyService(db, entitlements=ent, resolver_cache=tiered(KeyFamily.POLICY))
    )
    learner = LearnedSelector(state)
    log = logging.getLogger("aura.workers.autopilot")
    while True:
        for action in await autopilot.tick():
            log.info("autopilot %s", action)
        async with db.system() as session:
            consenting = list(
                await session.scalars(select(Account.id).where(Account.learning_consent.is_(True)))
            )
        await learner.publish_prior(await aggregate_prior(state, consenting, env=s.env.value))
        await asyncio.sleep(300)


async def _credits() -> None:
    """Monthly credit grants (idempotent per period, so an hourly loop is safe) and the
    consumer margin check (spec §76: alert when B2C margin goes negative)."""
    from datetime import UTC, datetime

    from aura_billing.application.consumer import CreditGate, MonthlyGrants
    from aura_billing.application.credits import CreditService
    from aura_billing.application.economics import UnitEconomics
    from aura_billing.domain.periods import period_end

    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    state, cache_redis = pools.get(RedisRole.STATE), pools.get(RedisRole.CACHE)
    scripts = RedisScripts(state)
    bus = InvalidationBus(cache_redis, env=s.env.value)

    def tiered(family: KeyFamily) -> TieredCache:
        return TieredCache(
            family,
            l1=L1Cache(1_000),
            l2=RedisL2(cache_redis),
            scripts=scripts,
            publisher=bus.publish,
        )

    ent = EntitlementService(
        db, cache=tiered(KeyFamily.ENTITLEMENTS), scripts=scripts, catalog=load_catalog()
    )
    credits = CreditService(db)
    gate = CreditGate(credits, ent, tiered(KeyFamily.CREDIT_BALANCE))
    grants = MonthlyGrants(db, credits, gate, ent)
    economics = UnitEconomics(db)
    log = logging.getLogger("aura.workers.credits")
    while True:
        now = datetime.now(UTC)
        granted = await grants.run(now=now)
        if granted:
            log.info("granted monthly credits to %d accounts", len(granted))
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = period_end("month", now)
        assert end is not None
        report = (await economics.consumer_margin(start, end)).to_dict()
        (log.error if report["alert"] else log.info)("consumer margin %s", report)
        await asyncio.sleep(3600)


async def _outbox_relay() -> None:
    s = get_settings()
    db, pools = Database(s.database_url), RedisPools(s)
    bus = InvalidationBus(pools.get(RedisRole.CACHE), env=s.env.value)
    await OutboxRelay(db, invalidation_handler(bus.publish)).run()


async def _migrate_clickhouse() -> None:
    s = get_settings()
    async with httpx.AsyncClient() as http:
        for path in sorted(DDL_DIR.glob("*.sql")):
            for stmt in [x.strip() for x in path.read_text().split(";")]:
                lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
                sql = "\n".join(lines).strip()
                if sql:
                    r = await http.post(s.clickhouse_url, content=sql.encode(), timeout=60)
                    r.raise_for_status()
            print(f"applied {path.name}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser("aura-workers")
    commands = {
        "telemetry": _telemetry,
        "jobs": _jobs,
        "outbox-relay": _outbox_relay,
        "canary-monitor": _canary_monitor,
        "autopilot": _autopilot,
        "credits": _credits,
        "migrate-clickhouse": _migrate_clickhouse,
    }
    parser.add_argument("command", choices=sorted(commands))
    args = parser.parse_args()
    asyncio.run(commands[args.command]())


if __name__ == "__main__":
    main()
