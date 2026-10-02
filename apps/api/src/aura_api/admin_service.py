"""Superadmin read models and actions (spec §33-36, §73-75). Cross-tenant by design;
every write is audited."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select

from aura_api.container import Container
from aura_billing.domain.ledger import TxnKind
from aura_db import audit
from aura_db.models import (
    Account,
    AuditLog,
    EntitlementOverride,
    Lead,
    Membership,
    Organization,
    Plan,
    PlanVersion,
    ShadowRun,
    Subscription,
    User,
    XRayReport,
)
from aura_shared.errors import NotFound, ValidationFailed
from aura_shared.ids import new_id

LIVE = ("trialing", "active", "past_due", "paused")


class AdminService:
    def __init__(self, c: Container) -> None:
        self.c = c

    # ---- metrics -------------------------------------------------------------------
    async def metrics(self) -> dict[str, Any]:
        async with self.c.db.system() as s:
            users = await s.scalar(select(func.count()).select_from(User)) or 0
            by_kind = dict(
                (await s.execute(select(Account.kind, func.count()).group_by(Account.kind))).all()
            )
            suspended = (
                await s.scalar(
                    select(func.count()).select_from(Account).where(Account.status == "suspended")
                )
                or 0
            )
            subs = (
                await s.execute(
                    select(
                        Plan.code,
                        Plan.audience,
                        Subscription.status,
                        PlanVersion.price_minor,
                        PlanVersion.currency,
                        func.count(),
                    )
                    .join(PlanVersion, PlanVersion.id == Subscription.plan_version_id)
                    .join(Plan, Plan.code == PlanVersion.plan_code)
                    .where(Subscription.status.in_(LIVE))
                    .group_by(
                        Plan.code,
                        Plan.audience,
                        Subscription.status,
                        PlanVersion.price_minor,
                        PlanVersion.currency,
                    )
                )
            ).all()
            leads = await s.scalar(select(func.count()).select_from(Lead)) or 0
            xrays = await s.scalar(select(func.count()).select_from(XRayReport)) or 0
            shadows = await s.scalar(select(func.count()).select_from(ShadowRun)) or 0
        plans: dict[str, int] = {}
        mrr = {"b2c": Decimal(0), "b2b": Decimal(0)}
        for code, audience, status, price, _currency, n in subs:
            plans[code] = plans.get(code, 0) + n
            if status == "active" and price:  # trials and past-due are not revenue
                mrr[audience] += Decimal(price) * n / 100
        return {
            "users": users,
            "b2c_accounts": by_kind.get("individual", 0),
            "organizations": by_kind.get("organization", 0),
            "suspended_accounts": suspended,
            "live_subscriptions_by_plan": plans,
            "mrr_inr": {k: str(v.quantize(Decimal("0.01"))) for k, v in mrr.items()}
            | {"total": str((mrr["b2c"] + mrr["b2b"]).quantize(Decimal("0.01")))},
            "arr_inr": str(((mrr["b2c"] + mrr["b2b"]) * 12).quantize(Decimal("0.01"))),
            "leads": leads,
            "xray_reports": xrays,
            "shadow_runs": shadows,
        }

    async def optimization_health(self) -> dict[str, Any]:
        """Spec §150: is the optimizer working, and where does it fail?"""
        from aura_db.models import Deployment, PolicyVersion

        async with self.c.db.system() as s:
            deployments = dict(
                (
                    await s.execute(
                        select(Deployment.status, func.count()).group_by(Deployment.status)
                    )
                ).all()
            )
            versions = dict(
                (
                    await s.execute(
                        select(PolicyVersion.status, func.count()).group_by(PolicyVersion.status)
                    )
                ).all()
            )
            shadows = dict(
                (
                    await s.execute(
                        select(ShadowRun.status, func.count()).group_by(ShadowRun.status)
                    )
                ).all()
            )
            autopilot = (
                await s.scalar(
                    select(func.count())
                    .select_from(Deployment)
                    .where(Deployment.created_by == "autopilot")
                )
                or 0
            )
            rollbacks = [
                {
                    "deployment_id": d.id,
                    "account_id": d.account_id,
                    "reason": d.rollback_reason,
                    "at": d.updated_at,
                }
                for d in await s.scalars(
                    select(Deployment)
                    .where(Deployment.status == "rolled_back")
                    .order_by(Deployment.updated_at.desc())
                    .limit(20)
                )
            ]
        completed = deployments.get("completed", 0)
        rolled = deployments.get("rolled_back", 0)
        return {
            "deployments_by_status": deployments,
            "policy_versions_by_status": versions,
            "shadow_runs_by_status": shadows,
            "autopilot_deployments": autopilot,
            "rollback_rate": round(rolled / (completed + rolled), 4) if completed + rolled else 0.0,
            "recent_rollbacks": rollbacks,
        }

    # ---- accounts --------------------------------------------------------------------
    async def list_accounts(
        self, *, kind: str | None, q: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        async with self.c.db.system() as s:
            stmt = (
                select(Account, Organization.name, Organization.slug, User.email)
                .outerjoin(Organization, Organization.account_id == Account.id)
                .outerjoin(User, User.id == Account.owner_user_id)
                .order_by(Account.created_at.desc())
                .limit(min(limit, 200))
                .offset(offset)
            )
            if kind:
                stmt = stmt.where(Account.kind == kind)
            if q:
                like = f"%{q.lower()}%"
                stmt = stmt.where(
                    or_(
                        func.lower(Organization.name).like(like),
                        func.lower(Organization.slug).like(like),
                        func.lower(User.email).like(like),
                        Account.id == q,
                    )
                )
            rows = (await s.execute(stmt)).all()
            plans = dict(
                (
                    await s.execute(
                        select(Subscription.account_id, PlanVersion.plan_code)
                        .join(PlanVersion, PlanVersion.id == Subscription.plan_version_id)
                        .where(
                            Subscription.status.in_(LIVE),
                            Subscription.account_id.in_([a.id for a, *_ in rows]),
                        )
                    )
                ).all()
            )
        return [
            {
                "account_id": a.id,
                "kind": a.kind,
                "tier": a.tier,
                "status": a.status,
                "name": org_name or email,
                "slug": slug,
                "plan": plans.get(a.id),
                "created_at": a.created_at,
            }
            for a, org_name, slug, email in rows
        ]

    async def account_detail(self, account_id: str) -> dict[str, Any]:
        async with self.c.db.system() as s:
            account = await s.get(Account, account_id)
            if account is None:
                raise NotFound("account not found")
            org = await s.scalar(select(Organization).where(Organization.account_id == account_id))
            members = []
            if org is not None:
                members = [
                    {"user_id": u.id, "email": u.email, "role": m.role}
                    for m, u in (
                        await s.execute(
                            select(Membership, User)
                            .join(User, User.id == Membership.user_id)
                            .where(Membership.organization_id == org.id)
                        )
                    ).all()
                ]
            recent = [
                {"action": a.action, "actor": a.actor_id, "at": a.created_at}
                for a in await s.scalars(
                    select(AuditLog)
                    .where(AuditLog.account_id == account_id)
                    .order_by(AuditLog.created_at.desc())
                    .limit(20)
                )
            ]
        sub = await self.c.subscriptions.current(account_id)
        ents = await self.c.entitlements.get(account_id)
        usage = {}
        for feature in ("executions", "premium_model_calls", "api_requests", "shadow_runs"):
            if ents.allows(feature):
                used, limit = await self.c.entitlements.usage(account_id, feature)
                usage[feature] = {"used": used, "limit": limit}
        return {
            "account_id": account.id,
            "kind": account.kind,
            "tier": account.tier,
            "status": account.status,
            "region": account.region,
            "organization": {"id": org.id, "name": org.name, "slug": org.slug} if org else None,
            "subscription": None
            if sub is None
            else {"plan": sub.plan_code, "status": sub.status, "version": sub.plan_version},
            "usage": usage,
            "credits_balance": await self.c.credits.balance(account_id),
            "members": members,
            "recent_audit": recent,
        }

    async def set_account_status(
        self, account_id: str, *, status: str, reason: str, actor_id: str
    ) -> None:
        if status not in ("active", "suspended"):
            raise ValidationFailed("status must be active or suspended")
        if not reason.strip():
            raise ValidationFailed("a reason is required")
        async with self.c.db.system() as s:
            account = await s.get(Account, account_id)
            if account is None:
                raise NotFound("account not found")
            before = account.status
            account.status = status
            await audit.record(
                s,
                actor_type="superadmin",
                actor_id=actor_id,
                account_id=account_id,
                action=f"account.{'suspend' if status == 'suspended' else 'restore'}",
                target_type="account",
                target_id=account_id,
                before={"status": before},
                after={"status": status, "reason": reason},
            )
        # Enforcement is immediate on the runtime: cached key records carry the status.
        await self.c.api_keys.invalidate_account(account_id)
        await self.c.entitlements.invalidate_cache(account_id)

    async def grant_credits(
        self,
        account_id: str,
        *,
        amount: int,
        kind: str,
        reason: str,
        actor_id: str,
        reference: str | None,
    ) -> dict[str, Any]:
        if kind not in ("grant", "adjust", "refund"):
            raise ValidationFailed("kind must be grant, adjust or refund")
        if not reason.strip():
            raise ValidationFailed("a reason is required")
        result = await self.c.credits.post(
            account_id,
            TxnKind(kind),
            amount,
            reference=reference,
            memo=reason,
            allow_negative=kind == "adjust",
            actor=("superadmin", actor_id),
        )
        await self.c.credit_gate.invalidate(account_id)
        return {
            "transaction_id": result.transaction_id,
            "balance": result.balance,
            "duplicate": result.duplicate,
        }

    async def add_override(
        self,
        account_id: str,
        *,
        feature: str,
        enabled: bool,
        limit: int | None,
        period: str | None,
        expires_at: datetime | None,
        reason: str,
        actor_id: str,
    ) -> str:
        spec = self.c.catalog.features.get(feature)
        if spec is None:
            raise ValidationFailed(f"unknown feature {feature!r}")
        if spec.kind == "metered" and period not in ("day", "month"):
            raise ValidationFailed("metered features need period day or month")
        if not reason.strip():
            raise ValidationFailed("a reason is required")
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValidationFailed("expires_at must be in the future")
        override_id = new_id("ovr")
        async with self.c.db.system() as s:
            if await s.get(Account, account_id) is None:
                raise NotFound("account not found")
            s.add(
                EntitlementOverride(
                    id=override_id,
                    account_id=account_id,
                    feature_key=feature,
                    enabled=enabled,
                    limit=limit,
                    period=period if spec.kind == "metered" else None,
                    expires_at=expires_at,
                    reason=reason,
                    created_by=actor_id,
                )
            )
            await audit.record(
                s,
                actor_type="superadmin",
                actor_id=actor_id,
                account_id=account_id,
                action="entitlement.override",
                target_type="entitlement_override",
                target_id=override_id,
                after={"feature": feature, "enabled": enabled, "limit": limit, "reason": reason},
            )
            await self.c.entitlements.enqueue_invalidation(s, account_id)
        await self.c.entitlements.invalidate_cache(account_id)
        return override_id

    # ---- users -----------------------------------------------------------------------
    async def set_user_status(
        self, user_id: str, *, status: str, reason: str, actor_id: str
    ) -> None:
        if status not in ("active", "suspended"):
            raise ValidationFailed("status must be active or suspended")
        async with self.c.db.system() as s:
            user = await s.get(User, user_id)
            if user is None:
                raise NotFound("user not found")
            before = user.status
            user.status = status
            await audit.record(
                s,
                actor_type="superadmin",
                actor_id=actor_id,
                action=f"user.{status}",
                target_type="user",
                target_id=user_id,
                before={"status": before},
                after={"status": status, "reason": reason},
            )
        if status == "suspended":
            await self.c.sessions.revoke_all(user_id)

    # ---- sales -----------------------------------------------------------------------
    async def leads(self, *, stage: str | None, limit: int) -> list[dict[str, Any]]:
        async with self.c.db.system() as s:
            stmt = select(Lead).order_by(Lead.created_at.desc()).limit(min(limit, 500))
            if stage:
                stmt = stmt.where(Lead.stage == stage)
            return [
                {
                    "id": x.id,
                    "email": x.email,
                    "company": x.company,
                    "source": x.source,
                    "stage": x.stage,
                    "summary": x.summary,
                    "created_at": x.created_at,
                }
                for x in await s.scalars(stmt)
            ]

    async def update_lead(self, lead_id: str, *, stage: str, actor_id: str) -> None:
        stages = ("new", "contacted", "audit", "shadow", "pilot", "won", "lost")
        if stage not in stages:
            raise ValidationFailed(f"stage must be one of {', '.join(stages)}")
        async with self.c.db.system() as s:
            lead = await s.get(Lead, lead_id)
            if lead is None:
                raise NotFound("lead not found")
            before, lead.stage = lead.stage, stage
            await audit.record(
                s,
                actor_type="superadmin",
                actor_id=actor_id,
                action="lead.stage",
                target_type="lead",
                target_id=lead_id,
                before={"stage": before},
                after={"stage": stage},
            )

    async def audit_log(self, *, account_id: str | None, limit: int) -> list[dict[str, Any]]:
        async with self.c.db.system() as s:
            stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
            if account_id:
                stmt = stmt.where(AuditLog.account_id == account_id)
            return [
                {
                    "id": a.id,
                    "at": a.created_at,
                    "actor_type": a.actor_type,
                    "actor_id": a.actor_id,
                    "account_id": a.account_id,
                    "action": a.action,
                    "target_type": a.target_type,
                    "target_id": a.target_id,
                    "before": a.before,
                    "after": a.after,
                }
                for a in await s.scalars(stmt)
            ]
