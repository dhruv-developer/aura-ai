"""Consumer (B2C) credit economics: the hot-path gate, usage metering, monthly grants
and referral rewards.

An account is *credit-metered* when its plan has the ``monthly_credits`` feature (the
consumer plans). Business accounts are invoiced on subscription and usage instead, so
none of this applies to them.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_billing.application.credits import CreditService
from aura_billing.application.entitlements import EntitlementService
from aura_billing.domain.catalog import Catalog
from aura_billing.domain.credits import billable, credits_for_cost
from aura_billing.domain.ledger import TxnKind
from aura_billing.domain.periods import period_label
from aura_billing.domain.subscriptions import LIVE
from aura_cache import TieredCache
from aura_db import Database
from aura_db.models import (
    Account,
    CreditWallet,
    LedgerEntry,
    LedgerTransaction,
    Subscription,
    User,
)
from aura_shared.errors import Conflict, NotFound, ValidationFailed
from aura_shared.redis import KeyFamily

log = logging.getLogger("aura.billing.consumer")

METERED_FEATURE = "monthly_credits"


class CreditGate:
    """Answers "may this account use AURA's platform keys right now?" from a short-lived
    cached balance. The cache only gates; the ledger stays the source of truth, so an
    account can overdraw by at most the calls admitted within one cache TTL."""

    def __init__(
        self, credits: CreditService, entitlements: EntitlementService, cache: TieredCache
    ) -> None:
        if cache.family is not KeyFamily.CREDIT_BALANCE:
            raise ValueError("CreditGate needs a TieredCache for KeyFamily.CREDIT_BALANCE")
        self._credits = credits
        self._ent = entitlements
        self._cache = cache

    async def metered(self, account_id: str) -> bool:
        return (await self._ent.get(account_id)).allows(METERED_FEATURE)

    async def balance(self, account_id: str) -> int:
        async def load() -> int:
            return await self._credits.balance(account_id)

        return int(await self._cache.get_or_load(account_id, "bal", loader=load))

    async def exhausted(self, account_id: str) -> bool:
        return await self.metered(account_id) and await self.balance(account_id) <= 0

    async def invalidate(self, account_id: str) -> None:
        await self._cache.invalidate(account_id, "bal")


class CreditMeter:
    """Charges platform-key inference to consumer accounts. Idempotent per execution
    (the ledger reference is unique per account), so redelivery cannot double-charge.
    Overdraft is allowed: the call already happened; the gate blocks the next one."""

    def __init__(self, credits: CreditService, gate: CreditGate) -> None:
        self._credits = credits
        self._gate = gate

    async def charge(
        self, *, account_id: str, execution_id: str, cost_usd: Decimal, credential_source: str
    ) -> int:
        if not billable(credential_source) or not await self._gate.metered(account_id):
            return 0
        amount = credits_for_cost(cost_usd)
        if amount == 0:
            return 0
        result = await self._credits.post(
            account_id,
            TxnKind.CONSUME,
            amount,
            reference=f"exec:{execution_id}",
            memo="inference",
            allow_negative=True,
        )
        if not result.duplicate:
            await self._gate.invalidate(account_id)
        return 0 if result.duplicate else amount


@dataclass(frozen=True, slots=True)
class GrantLine:
    account_id: str
    granted: int
    expired: int


class MonthlyGrants:
    """Grants each consumer account its plan's monthly credits once per period.

    Unused *granted* credits expire at the next grant (purchased and referral credits
    never expire): the expiry is capped at the previous grant, so a top-up is never
    taken away. Re-running for the same period is a no-op (ledger references).
    """

    def __init__(
        self,
        db: Database,
        credits: CreditService,
        gate: CreditGate,
        entitlements: EntitlementService,
        catalog: Catalog | None = None,
    ) -> None:
        self._db = db
        self._credits = credits
        self._gate = gate
        self._ent = entitlements
        self._catalog = catalog

    async def on_account_created(self, s: AsyncSession, account_id: str, kind: str) -> None:
        """Signup hook (runs in the signup transaction, after the subscription hook): the
        first month's credits, so a new user can try AURA immediately."""
        if self._catalog is None:
            return
        plan = self._catalog.default_plan_for(kind)
        ent = next((e for e in plan.entitlements if e.feature == METERED_FEATURE), None)
        if ent is None or not ent.enabled or not ent.limit:
            return
        await self._credits.post_in(
            s,
            account_id,
            TxnKind.GRANT,
            ent.limit,
            reference=f"grant:{period_label('month', datetime.now(UTC))}",
            memo="monthly credits",
        )

    async def run(self, *, now: datetime | None = None) -> list[GrantLine]:
        now = now or datetime.now(UTC)
        async with self._db.system() as s:
            account_ids = list(
                await s.scalars(
                    select(Account.id)
                    .join(Subscription, Subscription.account_id == Account.id)
                    .where(
                        Account.kind == "individual",
                        Account.status == "active",
                        Subscription.status.in_([st.value for st in LIVE]),
                    )
                    .order_by(Account.id)
                )
            )
        lines = []
        for account_id in account_ids:
            try:
                line = await self.grant(account_id, now=now)
            except Exception:  # one bad account must not stop the batch
                log.exception("monthly grant failed for %s", account_id)
                continue
            if line is not None:
                lines.append(line)
        return lines

    async def grant(self, account_id: str, *, now: datetime) -> GrantLine | None:
        ent = (await self._ent.get(account_id)).get(METERED_FEATURE)
        if ent is None or not ent.enabled or not ent.limit:
            return None
        label = period_label("month", now)
        reference = f"grant:{label}"
        expired = 0
        async with self._db.tenant(account_id) as s:
            if await s.scalar(
                select(LedgerTransaction.id).where(LedgerTransaction.reference == reference)
            ):
                return None
            previous = (
                await s.execute(
                    select(
                        LedgerTransaction.reference,
                        LedgerTransaction.created_at,
                        LedgerEntry.amount,
                    )
                    .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
                    .where(
                        LedgerTransaction.reference.like("grant:%"),
                        LedgerEntry.book == "customer",
                    )
                    .order_by(LedgerTransaction.created_at.desc())
                    .limit(1)
                )
            ).first()
            if previous is not None:
                prev_reference, prev_at, prev_amount = previous
                # Spending draws on granted credits first, so what is left of the grant is
                # the grant minus net usage since it (and never more than the balance).
                used = -(
                    await s.scalar(
                        select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                        .join(
                            LedgerTransaction,
                            LedgerTransaction.id == LedgerEntry.transaction_id,
                        )
                        .where(
                            LedgerEntry.book == "customer",
                            LedgerTransaction.kind.in_(["consume", "refund"]),
                            LedgerTransaction.created_at >= prev_at,
                        )
                    )
                    or 0
                )
                balance = (
                    await s.scalar(
                        select(CreditWallet.balance).where(CreditWallet.account_id == account_id)
                    )
                ) or 0
                expired = max(0, min(balance, prev_amount - max(0, used)))
                if expired:
                    await self._credits.post_in(
                        s,
                        account_id,
                        TxnKind.EXPIRE,
                        expired,
                        reference=f"expire:{prev_reference}",
                        memo="unused monthly credits",
                    )
            await self._credits.post_in(
                s,
                account_id,
                TxnKind.GRANT,
                ent.limit,
                reference=reference,
                memo="monthly credits",
            )
        await self._gate.invalidate(account_id)
        return GrantLine(account_id, ent.limit, expired)


REFERRAL_WINDOW = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class ReferralRewards:
    referee: int = 2_000
    referrer: int = 2_000
    referrer_monthly_cap: int = 20


class ReferralService:
    """Invite codes (spec §82). Both sides get credits once the new user redeems a code
    within ``REFERRAL_WINDOW`` of signing up. Abuse limits: no self-referral, one
    redemption per user, and a monthly cap on a referrer's rewards."""

    def __init__(
        self,
        db: Database,
        credits: CreditService,
        gate: CreditGate,
        rewards: ReferralRewards | None = None,
    ) -> None:
        self._db = db
        self._credits = credits
        self._gate = gate
        self._rewards = rewards or ReferralRewards()

    async def code_for(self, user_id: str) -> str:
        async with self._db.system() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is None:
                raise NotFound("user not found")
            if user.referral_code is None:
                user.referral_code = secrets.token_hex(4).upper()
            return user.referral_code

    @staticmethod
    async def _personal_account(s: AsyncSession, user_id: str) -> str:
        account_id = await s.scalar(
            select(Account.id).where(Account.owner_user_id == user_id, Account.kind == "individual")
        )
        if account_id is None:
            raise NotFound("personal account not found")
        return account_id

    async def stats(self, user_id: str) -> dict[str, int]:
        async with self._db.system() as s:
            account_id = await self._personal_account(s, user_id)
            rows = (
                await s.execute(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(
                        LedgerTransaction.account_id == account_id,
                        LedgerTransaction.reference.like("referral:%:referrer"),
                    )
                )
            ).scalar_one()
        return {"referred": int(rows), "credits_earned": int(rows) * self._rewards.referrer}

    async def redeem(self, *, user_id: str, code: str, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        code = code.strip().upper()
        if not code or len(code) > 16:
            raise ValidationFailed("invalid referral code")
        async with self._db.system() as s:
            user = await s.get(User, user_id)
            referrer = await s.scalar(select(User).where(User.referral_code == code))
            if user is None or referrer is None:
                raise NotFound("referral code not found")
            if referrer.id == user.id:
                raise ValidationFailed("you cannot refer yourself")
            if now - user.created_at > REFERRAL_WINDOW:
                raise ValidationFailed("referral codes must be redeemed soon after signup")
            referee_acct = await self._personal_account(s, user.id)
            referrer_acct = await self._personal_account(s, referrer.id)
            if await s.scalar(
                select(LedgerTransaction.id).where(
                    LedgerTransaction.account_id == referee_acct,
                    LedgerTransaction.reference.like("referral:%:referee"),
                )
            ):
                raise Conflict("a referral code was already redeemed")
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            earned = (
                await s.execute(
                    select(func.count())
                    .select_from(LedgerTransaction)
                    .where(
                        LedgerTransaction.account_id == referrer_acct,
                        LedgerTransaction.reference.like("referral:%:referrer"),
                        LedgerTransaction.created_at >= month_start,
                    )
                )
            ).scalar_one()
            await self._credits.post_in(
                s,
                referee_acct,
                TxnKind.GRANT,
                self._rewards.referee,
                reference=f"referral:{user.id}:referee",
                memo=f"referral bonus (code {code})",
            )
            if earned < self._rewards.referrer_monthly_cap:
                await self._credits.post_in(
                    s,
                    referrer_acct,
                    TxnKind.GRANT,
                    self._rewards.referrer,
                    reference=f"referral:{user.id}:referrer",
                    memo="referral reward",
                )
        await self._gate.invalidate(referee_acct)
        await self._gate.invalidate(referrer_acct)
        return self._rewards.referee
