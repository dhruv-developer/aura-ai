"""``aura-api`` entrypoint: ``serve`` (default) | ``create-superadmin`` | ``seed-demo`` |
``login-link`` (the last two are dev/test only)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from aura_api.container import Container


async def _create_superadmin(email: str) -> None:
    from sqlalchemy import func, select

    from aura_auth.domain.emails import normalize_email
    from aura_db import Database, audit
    from aura_db.models import User
    from aura_shared.ids import new_id
    from aura_shared.settings import get_settings

    email = normalize_email(email)
    db = Database(get_settings().database_url)
    try:
        async with db.system() as s:
            user = await s.scalar(select(User).where(func.lower(User.email) == email))
            if user is None:
                user = User(id=new_id("usr"), email=email)
                s.add(user)
            user.is_superadmin = True
            await s.flush()
            await audit.record(
                s,
                actor_type="system",
                actor_id="cli",
                action="admin.grant",
                target_type="user",
                target_id=user.id,
            )
            print(f"{email} is now a superadmin ({user.id}). Enroll TOTP at first admin login.")
    finally:
        await db.dispose()


# ---- local demo data (dev/test only) -----------------------------------------------------

DEMO_PERSONAL = (
    ("free@demo.aura", "free"),
    ("plus@demo.aura", "plus"),
    ("pro@demo.aura", "pro"),
)
DEMO_ORG = "Acme (demo)"
DEMO_ORG_PLAN = "growth"
DEMO_MEMBERS = (
    ("admin@acme.demo", "admin"),
    ("dev@acme.demo", "engineer"),
    ("viewer@acme.demo", "viewer"),
)
DEMO_OWNER = "owner@acme.demo"
DEMO_SUPERADMIN = "admin@aura.demo"


def _dev_container() -> Container:
    from aura_api.container import Container
    from aura_shared.settings import Environment, get_settings

    settings = get_settings()
    if settings.env not in (Environment.DEV, Environment.TEST):
        raise SystemExit("demo tooling is disabled outside dev/test")
    return Container.build(settings)


async def _seed_demo() -> None:
    """Idempotent: re-running keeps existing accounts and only fills gaps."""
    from sqlalchemy import update

    from aura_auth.application.dto import AccountAccess
    from aura_db.models import User
    from aura_shared.errors import Conflict

    c = _dev_container()
    rows: list[tuple[str, str]] = []
    try:
        for email, plan in DEMO_PERSONAL:
            user = await c.identity.login_with_email(email)
            [personal] = [
                a for a in await c.identity.list_access(user.id) if a.kind == "individual"
            ]
            sub = await c.subscriptions.current(personal.account_id)
            if sub is None or sub.plan_code != plan:
                await c.subscriptions.change_plan(
                    account_id=personal.account_id,
                    plan_code=plan,
                    actor_type="system",
                    actor_id="seed-demo",
                    reason="local demo data",
                )
            rows.append((email, f"personal account on {plan.title()}"))

        owner = await c.identity.login_with_email(DEMO_OWNER)
        access = await c.identity.list_access(owner.id)
        org: AccountAccess | None = next((a for a in access if a.name == DEMO_ORG), None)
        api_key: str | None = None
        if org is None:
            created = await c.identity.create_organization(owner.id, DEMO_ORG)
            org = await c.identity.resolve_access(owner.id, created.account_id)
            _, api_key = await c.api_keys.issue(
                account_id=org.account_id,
                actor_user_id=owner.id,
                actor_permissions=org.permissions,
                name="demo",
                permissions={"runtime:execute", "telemetry:write"},
                environment_id=created.environment_ids["production"],
            )
        sub = await c.subscriptions.current(org.account_id)
        if sub is None or sub.plan_code != DEMO_ORG_PLAN:
            await c.subscriptions.change_plan(
                account_id=org.account_id,
                plan_code=DEMO_ORG_PLAN,
                actor_type="system",
                actor_id="seed-demo",
                reason="local demo data",
            )
        rows.append((DEMO_OWNER, f"owner of '{DEMO_ORG}' (Growth plan)"))
        for email, role in DEMO_MEMBERS:
            with contextlib.suppress(Conflict):  # already a member from an earlier run
                await c.members.add_member(
                    None, "seed-demo", org.account_id, email=email, role=role
                )
            rows.append((email, f"{role} in '{DEMO_ORG}'"))

        await c.identity.login_with_email(DEMO_SUPERADMIN)
        async with c.db.system() as s:
            await s.execute(
                update(User).where(User.email == DEMO_SUPERADMIN).values(is_superadmin=True)
            )
        rows.append((DEMO_SUPERADMIN, "superadmin (admin console; TOTP set up on first login)"))

        width = max(len(e) for e, _ in rows)
        print("\nDemo accounts (passwordless: sign in with a link)\n")
        for email, what in rows:
            print(f"  {email.ljust(width)}  {what}")
        if api_key:
            print(f"\n  API key for '{DEMO_ORG}' (shown once): {api_key}")
        print("\nGet a sign-in link:  uv run aura-api login-link --email <email> [--app admin]")
    finally:
        await c.close()


async def _login_link(email: str, app: str) -> None:
    c = _dev_container()
    try:
        print(await c.magic_links.issue_link(email, app=app))
    finally:
        await c.close()


def main() -> None:
    parser = argparse.ArgumentParser("aura-api")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve")
    grant = sub.add_parser("create-superadmin")
    grant.add_argument("--email", required=True)
    sub.add_parser("seed-demo", help="create demo accounts (dev/test only)")
    link = sub.add_parser("login-link", help="print a one-time sign-in link (dev/test only)")
    link.add_argument("--email", required=True)
    link.add_argument("--app", choices=["web", "admin"], default="web")
    args = parser.parse_args()
    if args.command == "create-superadmin":
        asyncio.run(_create_superadmin(args.email))
        return
    if args.command == "seed-demo":
        asyncio.run(_seed_demo())
        return
    if args.command == "login-link":
        asyncio.run(_login_link(args.email, args.app))
        return
    uvicorn.run("aura_api.app:create_app", factory=True, host="0.0.0.0", port=8000)  # noqa: S104


if __name__ == "__main__":
    main()
