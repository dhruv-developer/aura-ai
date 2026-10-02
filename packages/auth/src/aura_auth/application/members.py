"""Organization members and custom roles (enterprise RBAC, spec §115).

Rules that hold for every path (dashboard, SCIM, SSO just-in-time provisioning):
- nobody grants permissions they do not hold, and only owners create owners;
- an organization always keeps at least one owner;
- every change is audited.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_auth.application.dto import AccountAccess
from aura_auth.application.identity import IdentityService
from aura_auth.domain.rbac import (
    Permission,
    Role,
    check_assignable,
    permissions_for,
    validate_custom_role,
)
from aura_db import Database, audit
from aura_db.models import CustomRole, Membership, Organization, User
from aura_shared.errors import Conflict, Forbidden, NotFound, ValidationFailed
from aura_shared.ids import new_id


@dataclass(frozen=True, slots=True)
class MemberView:
    user_id: str
    email: str
    name: str | None
    role: str
    custom_role_id: str | None
    source: str


@dataclass(frozen=True, slots=True)
class CustomRoleView:
    id: str
    name: str
    description: str
    permissions: list[str]


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError as exc:
        raise ValidationFailed(f"role must be one of {[r.value for r in Role]}") from exc


class MemberService:
    def __init__(self, db: Database, identity: IdentityService) -> None:
        self._db = db
        self._identity = identity

    @staticmethod
    async def _org(s: AsyncSession, account_id: str) -> Organization:
        org = await s.scalar(select(Organization).where(Organization.account_id == account_id))
        if org is None:
            raise NotFound("organization not found")
        return org

    # ---- custom roles ----------------------------------------------------------------
    async def list_roles(self, account_id: str) -> list[CustomRoleView]:
        async with self._db.tenant(account_id) as s:
            rows = await s.scalars(select(CustomRole).order_by(CustomRole.name))
            return [
                CustomRoleView(r.id, r.name, r.description, sorted(r.permissions)) for r in rows
            ]

    async def create_role(
        self,
        actor: AccountAccess,
        actor_user_id: str,
        *,
        name: str,
        permissions: set[str],
        description: str = "",
    ) -> CustomRoleView:
        name = name.strip()
        if not 2 <= len(name) <= 60:
            raise ValidationFailed("role name must be 2-60 characters")
        perms = validate_custom_role(permissions, actor.permissions)
        async with self._db.tenant(actor.account_id) as s:
            if await s.scalar(select(CustomRole.id).where(CustomRole.name == name)):
                raise Conflict("a role with this name exists")
            role = CustomRole(
                id=new_id("role"),
                account_id=actor.account_id,
                name=name,
                description=description[:300],
                permissions=sorted(p.value for p in perms),
            )
            s.add(role)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=actor.account_id,
                action="role.create",
                target_type="custom_role",
                target_id=role.id,
                after={"name": name, "permissions": role.permissions},
            )
            return CustomRoleView(role.id, name, role.description, role.permissions)

    async def delete_role(self, account_id: str, role_id: str, actor_user_id: str) -> None:
        """Members holding the role fall back to their base role."""
        async with self._db.tenant(account_id) as s:
            role = await s.get(CustomRole, role_id)
            if role is None:
                raise NotFound("role not found")
            await s.delete(role)
            await audit.record(
                s,
                actor_type="user",
                actor_id=actor_user_id,
                account_id=account_id,
                action="role.delete",
                target_type="custom_role",
                target_id=role_id,
                before={"name": role.name, "permissions": role.permissions},
            )

    # ---- members ---------------------------------------------------------------------
    async def list_members(self, account_id: str) -> list[MemberView]:
        async with self._db.system() as s:
            org = await self._org(s, account_id)
            rows = await s.execute(
                select(Membership, User)
                .join(User, User.id == Membership.user_id)
                .where(Membership.organization_id == org.id)
                .order_by(User.email)
            )
            return [
                MemberView(u.id, u.email, u.name, m.role, m.custom_role_id, m.source)
                for m, u in rows
            ]

    async def _granted(
        self, s: AsyncSession, account_id: str, role: Role, custom_role_id: str | None
    ) -> frozenset[Permission]:
        if custom_role_id is None:
            return permissions_for(role)
        custom = await s.get(CustomRole, custom_role_id)
        if custom is None or custom.account_id != account_id:
            raise NotFound("role not found")
        return frozenset(Permission(p) for p in custom.permissions)

    def _authorize(self, actor: AccountAccess, role: Role, granted: frozenset[Permission]) -> None:
        if role is Role.OWNER and actor.role != Role.OWNER:
            raise Forbidden("only an owner can make someone an owner")
        check_assignable(granted, actor.permissions)

    async def add_member(
        self,
        actor: AccountAccess | None,
        actor_id: str,
        account_id: str,
        *,
        email: str,
        role: str = "viewer",
        custom_role_id: str | None = None,
        name: str | None = None,
        source: str = "manual",
    ) -> MemberView:
        """``actor`` is None for trusted provisioning (SCIM, SSO), which is limited by
        the caller to non-owner roles."""
        base = _role(role)
        async with self._db.system() as s:
            org = await self._org(s, account_id)
            granted = await self._granted(s, account_id, base, custom_role_id)
            if actor is not None:
                self._authorize(actor, base, granted)
            elif base is Role.OWNER:
                raise Forbidden("provisioning cannot create owners")
            user = await self._identity.ensure_user(s, email, name)
            existing = await s.scalar(
                select(Membership).where(
                    Membership.organization_id == org.id, Membership.user_id == user.id
                )
            )
            if existing is not None:
                raise Conflict("already a member")
            m = Membership(
                id=new_id("mbr"),
                organization_id=org.id,
                user_id=user.id,
                role=base.value,
                custom_role_id=custom_role_id,
                source=source,
            )
            s.add(m)
            await audit.record(
                s,
                actor_type="user" if actor is not None else source,
                actor_id=actor_id,
                account_id=account_id,
                action="member.add",
                target_type="user",
                target_id=user.id,
                after={"role": base.value, "custom_role_id": custom_role_id, "source": source},
            )
            return MemberView(user.id, user.email, user.name, base.value, custom_role_id, source)

    async def _owners(self, s: AsyncSession, org_id: str) -> int:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.organization_id == org_id, Membership.role == Role.OWNER)
            )
            or 0
        )

    async def change_role(
        self,
        actor: AccountAccess | None,
        actor_id: str,
        account_id: str,
        user_id: str,
        *,
        role: str,
        custom_role_id: str | None = None,
    ) -> MemberView:
        base = _role(role)
        async with self._db.system() as s:
            org = await self._org(s, account_id)
            m = await s.scalar(
                select(Membership)
                .where(Membership.organization_id == org.id, Membership.user_id == user_id)
                .with_for_update()
            )
            if m is None:
                raise NotFound("member not found")
            granted = await self._granted(s, account_id, base, custom_role_id)
            if actor is not None:
                self._authorize(actor, base, granted)
                if m.role == Role.OWNER and actor.role != Role.OWNER:
                    raise Forbidden("only an owner can change an owner's role")
            elif base is Role.OWNER or m.role == Role.OWNER:
                raise Forbidden("provisioning cannot create or change owners")
            if (
                m.role == Role.OWNER
                and base is not Role.OWNER
                and await self._owners(s, org.id) <= 1
            ):
                raise Conflict("an organization needs at least one owner")
            before = {"role": m.role, "custom_role_id": m.custom_role_id}
            m.role, m.custom_role_id = base.value, custom_role_id
            await audit.record(
                s,
                actor_type="user" if actor is not None else "provisioning",
                actor_id=actor_id,
                account_id=account_id,
                action="member.change_role",
                target_type="user",
                target_id=user_id,
                before=before,
                after={"role": base.value, "custom_role_id": custom_role_id},
            )
            user = await s.get(User, user_id)
            assert user is not None
            return MemberView(user.id, user.email, user.name, m.role, m.custom_role_id, m.source)

    async def remove_member(
        self, actor: AccountAccess | None, actor_id: str, account_id: str, user_id: str
    ) -> None:
        async with self._db.system() as s:
            org = await self._org(s, account_id)
            m = await s.scalar(
                select(Membership)
                .where(Membership.organization_id == org.id, Membership.user_id == user_id)
                .with_for_update()
            )
            if m is None:
                raise NotFound("member not found")
            if m.role == Role.OWNER:
                if actor is None or actor.role != Role.OWNER:
                    raise Forbidden("only an owner can remove an owner")
                if await self._owners(s, org.id) <= 1:
                    raise Conflict("an organization needs at least one owner")
            await s.delete(m)
            await audit.record(
                s,
                actor_type="user" if actor is not None else "provisioning",
                actor_id=actor_id,
                account_id=account_id,
                action="member.remove",
                target_type="user",
                target_id=user_id,
                before={"role": m.role},
            )
