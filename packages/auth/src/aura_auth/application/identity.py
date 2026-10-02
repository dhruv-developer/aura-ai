"""Users, personal accounts, organizations and access resolution."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_auth.application.dto import AccountAccess, OrganizationView, UserView
from aura_auth.domain.emails import normalize_email, slugify
from aura_auth.domain.rbac import (
    CUSTOM_ROLE_GRANTABLE,
    PERSONAL_ACCOUNT_PERMISSIONS,
    Permission,
    Role,
    permissions_for,
)
from aura_db import Database, audit
from aura_db.models import (
    Account,
    CustomRole,
    Environment,
    Membership,
    OAuthIdentity,
    Organization,
    Project,
    User,
    Workspace,
)
from aura_shared.errors import Conflict, Forbidden, NotFound, ValidationFailed
from aura_shared.ids import new_id

# Called inside the account-creating transaction (e.g. billing starts the default plan).
AccountCreatedHook = Callable[[AsyncSession, str, str], Awaitable[None]]

ENVIRONMENT_KINDS = ("development", "staging", "production")


def _view(u: User) -> UserView:
    return UserView(u.id, u.email, u.name, u.status, u.is_superadmin)


class IdentityService:
    def __init__(
        self, db: Database, *, region: str, hooks: Sequence[AccountCreatedHook] = ()
    ) -> None:
        self._db = db
        self._region = region
        self._hooks = list(hooks)

    def add_hook(self, hook: AccountCreatedHook) -> None:
        self._hooks.append(hook)

    # ---- users -----------------------------------------------------------------------
    async def get_user(self, user_id: str) -> UserView:
        async with self._db.system() as s:
            user = await s.get(User, user_id)
        if user is None:
            raise NotFound("user not found")
        return _view(user)

    async def login_with_email(self, raw_email: str, *, name: str | None = None) -> UserView:
        """Called after the magic link proved control of the address."""
        email = normalize_email(raw_email)
        async with self._db.system() as s:
            user = await self._user_by_email(s, email)
            if user is None:
                user = await self._create_user(s, email, name)
            self._check_active(user)
            now = datetime.now(UTC)
            user.email_verified_at = user.email_verified_at or now
            user.last_login_at = now
            return _view(user)

    async def login_with_oauth(
        self, *, provider: str, subject: str, email: str, email_verified: bool, name: str | None
    ) -> UserView:
        email = normalize_email(email)
        async with self._db.system() as s:
            ident = await s.scalar(
                select(OAuthIdentity).where(
                    OAuthIdentity.provider == provider, OAuthIdentity.subject == subject
                )
            )
            if ident is not None:
                user = await s.get(User, ident.user_id)
                assert user is not None
            else:
                user = await self._user_by_email(s, email)
                if user is not None and not email_verified:
                    # Linking an unverified provider email to an existing user would allow
                    # account takeover.
                    raise Conflict("email already registered; sign in with email first")
                if user is None:
                    user = await self._create_user(s, email, name)
                s.add(
                    OAuthIdentity(
                        id=new_id("oid"),
                        user_id=user.id,
                        provider=provider,
                        subject=subject,
                        email=email,
                    )
                )
            self._check_active(user)
            now = datetime.now(UTC)
            if email_verified:
                user.email_verified_at = user.email_verified_at or now
            user.last_login_at = now
            return _view(user)

    async def ensure_user(self, s: AsyncSession, raw_email: str, name: str | None = None) -> User:
        """Find or create (with a personal account) the user for an email, inside the
        caller's transaction. Used when an admin, SCIM or SSO adds someone."""
        email = normalize_email(raw_email)
        user = await self._user_by_email(s, email)
        return user if user is not None else await self._create_user(s, email, name)

    @staticmethod
    def _check_active(user: User) -> None:
        if user.status != "active":
            raise Forbidden("account suspended")

    @staticmethod
    async def _user_by_email(s: AsyncSession, email: str) -> User | None:
        return await s.scalar(select(User).where(func.lower(User.email) == email))

    async def _create_user(self, s: AsyncSession, email: str, name: str | None) -> User:
        user = User(id=new_id("usr"), email=email, name=name)
        s.add(user)
        await s.flush()
        account = Account(
            id=new_id("acct"),
            kind="individual",
            tier="b2c",
            owner_user_id=user.id,
            region=self._region,
        )
        s.add(account)
        await s.flush()
        s.add(Workspace(id=new_id("ws"), account_id=account.id, name="Personal", is_default=True))
        await s.flush()
        for hook in self._hooks:
            await hook(s, account.id, "individual")
        await audit.record(
            s, actor_type="user", actor_id=user.id, account_id=account.id, action="user.signup"
        )
        return user

    # ---- organizations -----------------------------------------------------------------
    async def create_organization(self, user_id: str, name: str) -> OrganizationView:
        name = name.strip()
        if not 2 <= len(name) <= 200:
            raise ValidationFailed("organization name must be 2-200 characters")
        async with self._db.system() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise NotFound("user not found")
            self._check_active(user)
            slug = await self._unique_slug(s, slugify(name))
            account = Account(
                id=new_id("acct"), kind="organization", tier="b2b", region=self._region
            )
            s.add(account)
            await s.flush()
            org = Organization(id=new_id("org"), account_id=account.id, name=name, slug=slug)
            s.add(org)
            await s.flush()
            s.add(
                Membership(
                    id=new_id("mbr"), organization_id=org.id, user_id=user.id, role=Role.OWNER
                )
            )
            ws = Workspace(id=new_id("ws"), account_id=account.id, name=name, is_default=True)
            s.add(ws)
            await s.flush()
            project = Project(
                id=new_id("prj"),
                account_id=account.id,
                workspace_id=ws.id,
                name="Default",
                slug="default",
            )
            s.add(project)
            await s.flush()
            envs: dict[str, str] = {}
            for kind in ENVIRONMENT_KINDS:
                env = Environment(
                    id=new_id("env"), account_id=account.id, project_id=project.id, kind=kind
                )
                s.add(env)
                envs[kind] = env.id
            await s.flush()
            for hook in self._hooks:
                await hook(s, account.id, "organization")
            await audit.record(
                s,
                actor_type="user",
                actor_id=user.id,
                account_id=account.id,
                action="organization.create",
                target_type="organization",
                target_id=org.id,
                after={"name": name, "slug": slug},
            )
            return OrganizationView(org.id, account.id, name, slug, ws.id, project.id, envs)

    @staticmethod
    async def _unique_slug(s: AsyncSession, base: str) -> str:
        slug = base
        for _ in range(5):
            if await s.scalar(select(Organization.id).where(Organization.slug == slug)) is None:
                return slug
            slug = f"{base[:52]}-{secrets.token_hex(3)}"
        raise Conflict("could not allocate an organization slug")

    # ---- access ------------------------------------------------------------------------
    async def list_access(self, user_id: str) -> list[AccountAccess]:
        async with self._db.system() as s:
            out: list[AccountAccess] = []
            personal = await s.scalar(
                select(Account).where(
                    Account.owner_user_id == user_id, Account.kind == "individual"
                )
            )
            if personal is not None:
                out.append(self._personal_access(personal))
            rows = await s.execute(
                select(Membership, Organization, Account, CustomRole)
                .join(Organization, Organization.id == Membership.organization_id)
                .join(Account, Account.id == Organization.account_id)
                .outerjoin(CustomRole, CustomRole.id == Membership.custom_role_id)
                .where(Membership.user_id == user_id)
                .order_by(Organization.name)
            )
            for m, org, acct, custom in rows:
                perms = (
                    frozenset(Permission(p) for p in custom.permissions) & CUSTOM_ROLE_GRANTABLE
                    if custom is not None
                    else permissions_for(m.role)
                )
                out.append(
                    AccountAccess(
                        account_id=acct.id,
                        kind=acct.kind,
                        tier=acct.tier,
                        status=acct.status,
                        region=acct.region,
                        name=org.name,
                        role=f"custom:{custom.name}" if custom is not None else m.role,
                        permissions=perms,
                        organization_id=org.id,
                        slug=org.slug,
                    )
                )
            return out

    async def resolve_access(self, user_id: str, account_id: str) -> AccountAccess:
        """Raise NotFound (not Forbidden) so account existence is not leaked."""
        for access in await self.list_access(user_id):
            if access.account_id == account_id:
                if access.status != "active":
                    raise Forbidden("account suspended")
                return access
        raise NotFound("account not found")

    @staticmethod
    def _personal_access(acct: Account) -> AccountAccess:
        return AccountAccess(
            account_id=acct.id,
            kind=acct.kind,
            tier=acct.tier,
            status=acct.status,
            region=acct.region,
            name="Personal",
            role=Role.OWNER,
            permissions=PERSONAL_ACCOUNT_PERMISSIONS,
        )

    async def default_workspace_id(self, account_id: str) -> str:
        async with self._db.tenant(account_id) as s:
            ws = await s.scalar(select(Workspace.id).where(Workspace.is_default.is_(True)))
        if ws is None:
            raise NotFound("workspace not found")
        return ws
