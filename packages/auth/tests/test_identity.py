"""Integration tests: real Postgres (RLS on) + real Redis."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aura_auth.domain.rbac import Permission, Role
from aura_db.models import Workspace
from aura_shared.errors import (
    Conflict,
    NotFound,
    RateLimited,
    Unauthenticated,
    ValidationFailed,
)
from aura_shared.ids import new_id


async def test_email_login_is_idempotent_and_case_insensitive(container) -> None:  # type: ignore[no-untyped-def]
    a = await container.identity.login_with_email("Dev@Example.com")
    b = await container.identity.login_with_email("dev@example.COM")
    assert a.id == b.id
    [personal] = await container.identity.list_access(a.id)
    assert personal.kind == "individual" and personal.role == Role.OWNER
    assert Permission.BILLING_MANAGE in personal.permissions


async def test_create_org_provisions_tenant_skeleton(container) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("owner@acme.io")
    org = await container.identity.create_organization(user.id, "Acme AI")
    assert org.slug == "acme-ai"
    assert set(org.environment_ids) == {"development", "staging", "production"}
    org2 = await container.identity.create_organization(user.id, "Acme AI")
    assert org2.slug != org.slug and org2.slug.startswith("acme-ai-")
    access = await container.identity.resolve_access(user.id, org.account_id)
    assert access.role == "owner" and access.organization_id == org.id
    sub = await container.subscriptions.current(org.account_id)
    assert sub is not None and sub.plan_code == "startup" and sub.status == "trialing"
    with pytest.raises(ValidationFailed):
        await container.identity.create_organization(user.id, "x")


async def test_non_members_get_not_found(container) -> None:  # type: ignore[no-untyped-def]
    owner = await container.identity.login_with_email("owner@acme.io")
    stranger = await container.identity.login_with_email("eve@evil.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    with pytest.raises(NotFound):
        await container.identity.resolve_access(stranger.id, org.account_id)
    personal = next(
        a for a in await container.identity.list_access(owner.id) if a.kind == "individual"
    )
    with pytest.raises(NotFound):
        await container.identity.resolve_access(stranger.id, personal.account_id)


async def test_oauth_links_verified_email_and_blocks_unverified(container) -> None:  # type: ignore[no-untyped-def]
    existing = await container.identity.login_with_email("dev@x.io")
    with pytest.raises(Conflict):
        await container.identity.login_with_oauth(
            provider="google", subject="g-1", email="dev@x.io", email_verified=False, name=None
        )
    linked = await container.identity.login_with_oauth(
        provider="google", subject="g-1", email="dev@x.io", email_verified=True, name="Dev"
    )
    assert linked.id == existing.id
    again = await container.identity.login_with_oauth(
        provider="google", subject="g-1", email="changed@x.io", email_verified=True, name=None
    )
    assert again.id == existing.id  # identity is keyed on (provider, subject)
    fresh = await container.identity.login_with_oauth(
        provider="google", subject="g-2", email="new@x.io", email_verified=True, name="New"
    )
    assert fresh.id != existing.id


async def test_rls_isolates_tenants(container) -> None:  # type: ignore[no-untyped-def]
    a = await container.identity.login_with_email("a@x.io")
    b = await container.identity.login_with_email("b@x.io")
    [acct_a] = await container.identity.list_access(a.id)
    [acct_b] = await container.identity.list_access(b.id)

    async with container.db.tenant(acct_a.account_id) as s:
        visible = set(await s.scalars(select(Workspace.account_id)))
    assert visible == {acct_a.account_id}

    # WITH CHECK: tenant A cannot write a row owned by tenant B.
    with pytest.raises(DBAPIError, match="row-level security"):
        async with container.db.tenant(acct_a.account_id) as s:
            s.add(Workspace(id=new_id("ws"), account_id=acct_b.account_id, name="sneaky"))

    # No tenant bound at all -> nothing visible (fail closed).
    async with container.db.engine.connect() as conn:
        await conn.execute(text("SET ROLE aura_app"))
        assert (await conn.execute(text("SELECT count(*) FROM workspaces"))).scalar() == 0
        await conn.rollback()


async def test_magic_link_single_use_and_rate_limited(container) -> None:  # type: ignore[no-untyped-def]
    mailer = container.magic_links._mailer
    await container.magic_links.request("Dev@X.io", ip="1.2.3.4")
    [msg] = mailer.outbox
    assert msg.to == "dev@x.io" and "http://web.test/auth/verify?token=" in msg.text
    token = msg.text.split("token=")[1].split()[0]
    user = await container.magic_links.verify(token)
    assert user.email == "dev@x.io"
    with pytest.raises(Unauthenticated):
        await container.magic_links.verify(token)  # single use
    with pytest.raises(Unauthenticated):
        await container.magic_links.verify("forged")

    for _ in range(4):
        await container.magic_links.request("dev@x.io", ip="1.2.3.4")
    with pytest.raises(RateLimited):
        await container.magic_links.request("dev@x.io", ip="1.2.3.4")


async def test_sessions_revoke_and_revoke_all(container) -> None:  # type: ignore[no-untyped-def]
    user = await container.identity.login_with_email("s@x.io")
    c1 = await container.sessions.create(user.id, ip="1.1.1.1")
    c2 = await container.sessions.create(user.id)
    assert (await container.sessions.get(c1)).user_id == user.id
    assert await container.sessions.get("usr_bogus.secret") is None
    assert await container.sessions.get("garbage") is None
    assert await container.sessions.get(c1 + "x") is None
    await container.sessions.revoke(c1)
    assert await container.sessions.get(c1) is None
    assert await container.sessions.get(c2) is not None
    assert await container.sessions.revoke_all(user.id) == 1
    assert await container.sessions.get(c2) is None


async def test_api_key_lifecycle(container) -> None:  # type: ignore[no-untyped-def]
    owner = await container.identity.login_with_email("owner@acme.io")
    org = await container.identity.create_organization(owner.id, "Acme")
    access = await container.identity.resolve_access(owner.id, org.account_id)
    view, plaintext = await container.api_keys.issue(
        account_id=org.account_id,
        actor_user_id=owner.id,
        actor_permissions=access.permissions,
        name="prod runtime",
        permissions={"runtime:execute"},
        environment_id=org.environment_ids["production"],
    )
    assert plaintext.startswith("aura_sk_prod_")
    principal = await container.api_keys.authenticate(plaintext)
    assert principal.account_id == org.account_id
    assert principal.permissions == {"runtime:execute"}
    with pytest.raises(Unauthenticated):
        await container.api_keys.authenticate(plaintext[:-2] + "zz")

    [listed] = await container.api_keys.list(org.account_id)
    assert listed.prefix == view.prefix and listed.last_used_at is not None
    await container.api_keys.revoke(
        account_id=org.account_id, key_id=view.id, actor_user_id=owner.id
    )
    with pytest.raises(Unauthenticated, match="revoked"):
        await container.api_keys.authenticate(plaintext)


async def test_api_key_rejects_foreign_environment_and_past_expiry(container) -> None:  # type: ignore[no-untyped-def]
    a = await container.identity.login_with_email("a@acme.io")
    b = await container.identity.login_with_email("b@other.io")
    org_a = await container.identity.create_organization(a.id, "Acme")
    org_b = await container.identity.create_organization(b.id, "Other")
    access = await container.identity.resolve_access(a.id, org_a.account_id)
    common = {
        "account_id": org_a.account_id,
        "actor_user_id": a.id,
        "actor_permissions": access.permissions,
        "name": "k",
        "permissions": {"runtime:execute"},
    }
    with pytest.raises(NotFound):  # RLS hides org B's environment from org A
        await container.api_keys.issue(**common, environment_id=org_b.environment_ids["production"])
    with pytest.raises(ValidationFailed):
        await container.api_keys.issue(
            **common, environment_id=None, expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )


async def test_magic_link_app_selects_allowlisted_verify_url(container) -> None:  # type: ignore[no-untyped-def]
    await container.magic_links.request("ops@aura.ai", ip=None, app="admin")
    assert "/auth/verify?token=" in container.magic_links._mailer.outbox[-1].text
    assert container.settings.public_admin_url in container.magic_links._mailer.outbox[-1].text
    with pytest.raises(ValidationFailed):
        await container.magic_links.request("ops@aura.ai", ip=None, app="https://evil.example")
