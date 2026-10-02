"""Shared pytest fixtures: a real, throwaway redis-server per test session.

Integration tests use a real Redis (not a fake) so Lua scripts run on the real engine.
Set AURA_TEST_REDIS_URL to use an existing server (CI service container) instead.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator

import pytest
import redis
import redis.asyncio as aioredis

os.environ.setdefault("AURA_ENV", "test")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if url := os.environ.get("AURA_TEST_REDIS_URL"):
        yield url
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed and AURA_TEST_REDIS_URL not set")
    port = _free_port()
    workdir = tmp_path_factory.mktemp("redis")
    proc = subprocess.Popen(  # noqa: S603
        [binary, "--port", str(port), "--save", "", "--appendonly", "no", "--dir", str(workdir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    client = redis.Redis.from_url(url)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("redis-server did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(redis_url)
    await client.flushdb()  # test-only; application code never flushes (lint-banned)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------------------
# Postgres: a throwaway cluster per session, migrated once, catalog seeded once.
# Set AURA_TEST_DATABASE_URL (asyncpg URL, superuser) to use an existing server in CI.
# ---------------------------------------------------------------------------------------
CATALOG_TABLES = {"features", "plans", "plan_versions", "plan_entitlements", "alembic_version"}


def _migrate_and_seed(url: str) -> None:
    import asyncio
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from aura_billing.application.catalog_sync import sync_catalog
    from aura_billing.domain.catalog import load_catalog
    from aura_db import Database

    cfg = Config(str(Path(__file__).parent / "packages/db/alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    async def seed() -> None:
        db = Database(url)
        await sync_catalog(db, load_catalog())
        await db.dispose()

    asyncio.run(seed())


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if url := os.environ.get("AURA_TEST_DATABASE_URL"):
        _migrate_and_seed(url)
        yield url
        return
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if initdb is None or pg_ctl is None:
        pytest.skip("Postgres binaries not installed and AURA_TEST_DATABASE_URL not set")
    base = tmp_path_factory.mktemp("pg")
    data, port = base / "data", _free_port()
    subprocess.run(  # noqa: S603
        [initdb, "-D", str(data), "-U", "aura", "--auth=trust", "-E", "UTF8", "--locale=C"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    # TCP only: macOS temp paths exceed the 103-byte Unix-socket path limit.
    opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"
    subprocess.run(  # noqa: S603
        [pg_ctl, "-D", str(data), "-o", opts, "-l", str(base / "log"), "-w", "start"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        subprocess.run(  # noqa: S603
            [
                shutil.which("createdb") or "createdb",
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "aura",
                "aura",
            ],
            check=True,
        )
        url = f"postgresql+asyncpg://aura@127.0.0.1:{port}/aura"
        _migrate_and_seed(url)
        yield url
    finally:
        subprocess.run(  # noqa: S603
            [pg_ctl, "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            stdout=subprocess.DEVNULL,
        )


@pytest.fixture
async def clean_db(pg_url: str) -> AsyncIterator[str]:
    """Truncate every non-catalog table before the test (as the superuser, bypassing RLS)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        tables = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
        doomed = sorted(set(tables) - CATALOG_TABLES)
        await conn.execute(text(f"TRUNCATE {', '.join(doomed)} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    yield pg_url


@pytest.fixture
async def container(clean_db: str, redis_url: str, redis_client: aioredis.Redis):  # type: ignore[no-untyped-def]
    """A fully wired control-plane container on the test Postgres + Redis."""
    from aura_api.container import Container
    from aura_auth.adapters.mailer import InMemoryMailer
    from aura_shared.settings import Environment, Settings

    # Never read the developer's .env: tests must not depend on local keys or call real
    # model providers (e.g. a local Ollama configured for manual testing).
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env=Environment.TEST,
        database_url=clean_db,
        redis_state_url=redis_url,
        redis_cache_url=redis_url,
        redis_vector_url=redis_url,
        razorpay_webhook_secret="whsec_test",  # noqa: S106 - test value
        public_web_url="http://web.test",
        public_api_url="http://api.test",
    )
    c = Container.build(settings, mailer=InMemoryMailer())
    yield c
    await c.close()


@pytest.fixture
async def runtime(container):  # type: ignore[no-untyped-def]
    """Runtime container (data plane) on the same Postgres/Redis, stub models only."""
    from aura_engine import InMemoryPublisher
    from aura_runtime.container import RuntimeContainer

    settings = container.settings.model_copy(update={"stub_provider": True})
    rt = RuntimeContainer.build(settings, publisher=InMemoryPublisher())
    yield rt
    await rt.close()
