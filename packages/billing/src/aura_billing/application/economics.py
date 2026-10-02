"""Consumer unit economics (spec §76, §130): revenue minus inference cost, per account.

- Revenue: paid subscription invoices plus credit packs, in the period, in INR.
- Inference cost: credits consumed on AURA's platform keys, converted back to dollars
  (BYOK calls cost AURA nothing and are never charged credits).
- Infra: a flat per-active-account estimate, until real cost allocation exists.

A negative total margin on consumer plans is the alert condition: it means free usage
or underpriced plans are burning money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from aura_billing.domain.credits import CREDITS_PER_USD
from aura_db import Database
from aura_db.models import (
    Account,
    Invoice,
    LedgerEntry,
    LedgerTransaction,
    PaymentEvent,
)


@dataclass(frozen=True, slots=True)
class AccountMargin:
    account_id: str
    revenue_inr: Decimal
    inference_inr: Decimal
    infra_inr: Decimal

    @property
    def margin_inr(self) -> Decimal:
        return self.revenue_inr - self.inference_inr - self.infra_inr


@dataclass(frozen=True, slots=True)
class MarginReport:
    start: datetime
    end: datetime
    usd_inr: Decimal
    accounts: list[AccountMargin] = field(default_factory=list)

    @property
    def revenue_inr(self) -> Decimal:
        return sum((a.revenue_inr for a in self.accounts), Decimal(0))

    @property
    def cost_inr(self) -> Decimal:
        return sum((a.inference_inr + a.infra_inr for a in self.accounts), Decimal(0))

    @property
    def margin_inr(self) -> Decimal:
        return self.revenue_inr - self.cost_inr

    @property
    def alert(self) -> bool:
        return self.margin_inr < 0

    def to_dict(self, *, worst: int = 10) -> dict[str, object]:
        losing = sorted(self.accounts, key=lambda a: a.margin_inr)[:worst]
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "usd_inr": str(self.usd_inr),
            "accounts": len(self.accounts),
            "revenue_inr": str(self.revenue_inr.quantize(Decimal("0.01"))),
            "cost_inr": str(self.cost_inr.quantize(Decimal("0.01"))),
            "margin_inr": str(self.margin_inr.quantize(Decimal("0.01"))),
            "gross_margin_pct": (
                float(self.margin_inr / self.revenue_inr * 100) if self.revenue_inr else None
            ),
            "negative_margin_accounts": sum(1 for a in self.accounts if a.margin_inr < 0),
            "alert": self.alert,
            "worst": [
                {
                    "account_id": a.account_id,
                    "revenue_inr": str(a.revenue_inr.quantize(Decimal("0.01"))),
                    "cost_inr": str((a.inference_inr + a.infra_inr).quantize(Decimal("0.01"))),
                    "margin_inr": str(a.margin_inr.quantize(Decimal("0.01"))),
                }
                for a in losing
                if a.margin_inr < 0
            ],
        }


class UnitEconomics:
    def __init__(
        self,
        db: Database,
        *,
        usd_inr: Decimal = Decimal("84"),
        infra_inr_per_active_account: Decimal = Decimal("2"),
    ) -> None:
        self._db = db
        self._fx = usd_inr
        self._infra = infra_inr_per_active_account

    async def consumer_margin(self, start: datetime, end: datetime) -> MarginReport:
        async with self._db.system() as s:
            consumers = {
                a for a in await s.scalars(select(Account.id).where(Account.kind == "individual"))
            }
            invoices = dict(
                (
                    await s.execute(
                        select(Invoice.account_id, func.sum(Invoice.amount_minor))
                        .where(
                            Invoice.status == "paid",
                            Invoice.currency == "INR",
                            Invoice.issued_at >= start,
                            Invoice.issued_at < end,
                        )
                        .group_by(Invoice.account_id)
                    )
                ).all()
            )
            packs: dict[str, int] = {}
            payloads: list[dict[str, Any]] = list(
                await s.scalars(
                    select(PaymentEvent.payload).where(
                        PaymentEvent.type == "order.paid",
                        PaymentEvent.error.is_(None),
                        PaymentEvent.processed_at.is_not(None),
                        PaymentEvent.received_at >= start,
                        PaymentEvent.received_at < end,
                    )
                )
            )
            for payload in payloads:
                order = (payload.get("payload", {}).get("order") or {}).get("entity") or {}
                acct = (order.get("notes") or {}).get("account_id")
                if acct and (order.get("currency") or "").upper() == "INR":
                    packs[acct] = packs.get(acct, 0) + int(order.get("amount_paid") or 0)
            consumed = dict(
                (
                    await s.execute(
                        select(LedgerTransaction.account_id, func.sum(LedgerEntry.amount))
                        .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
                        .where(
                            LedgerTransaction.kind == "consume",
                            LedgerEntry.book == "customer",
                            LedgerTransaction.created_at >= start,
                            LedgerTransaction.created_at < end,
                        )
                        .group_by(LedgerTransaction.account_id)
                    )
                ).all()
            )
        report = MarginReport(start, end, self._fx)
        for acct in sorted(consumers & (set(invoices) | set(packs) | set(consumed))):
            revenue = Decimal(int(invoices.get(acct, 0)) + packs.get(acct, 0)) / 100
            credits_used = -int(consumed.get(acct, 0))  # customer-side deltas are negative
            inference = Decimal(credits_used) / CREDITS_PER_USD * self._fx
            infra = self._infra if credits_used else Decimal(0)
            report.accounts.append(AccountMargin(acct, revenue, inference, infra))
        return report
