"""Double-entry credit ledger with idempotent, serialised postings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aura_billing.domain.ledger import TxnKind, postings_for
from aura_db import Database, audit
from aura_db.models import CreditWallet, LedgerEntry, LedgerTransaction
from aura_shared.errors import QuotaExceeded, ValidationFailed
from aura_shared.ids import new_id


@dataclass(frozen=True, slots=True)
class PostingResult:
    transaction_id: str
    balance: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class LedgerLine:
    transaction_id: str
    kind: str
    amount: int  # customer-side delta
    memo: str
    reference: str | None
    created_at: datetime


class CreditService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def balance(self, account_id: str) -> int:
        async with self._db.tenant(account_id) as s:
            return (
                await s.scalar(
                    select(CreditWallet.balance).where(CreditWallet.account_id == account_id)
                )
            ) or 0

    async def post(
        self,
        account_id: str,
        kind: TxnKind,
        amount: int,
        *,
        reference: str | None = None,
        memo: str = "",
        allow_negative: bool = False,
        actor: tuple[str, str] | None = None,
    ) -> PostingResult:
        async with self._db.tenant(account_id) as s:
            return await self.post_in(
                s,
                account_id,
                kind,
                amount,
                reference=reference,
                memo=memo,
                allow_negative=allow_negative,
                actor=actor,
            )

    @staticmethod
    async def post_in(
        s: AsyncSession,
        account_id: str,
        kind: TxnKind,
        amount: int,
        *,
        reference: str | None = None,
        memo: str = "",
        allow_negative: bool = False,
        actor: tuple[str, str] | None = None,
    ) -> PostingResult:
        """Post inside the caller's transaction (e.g. a payment webhook), so the posting
        and the caller's other writes commit or roll back together."""
        customer, system = postings_for(kind, amount)
        if reference is not None and not 1 <= len(reference) <= 128:
            raise ValidationFailed("reference must be 1-128 characters")
        # Lock the wallet: serialises postings per account (and idempotency checks).
        await s.execute(
            insert(CreditWallet)
            .values(account_id=account_id, balance=0)
            .on_conflict_do_nothing(index_elements=[CreditWallet.account_id])
        )
        wallet = await s.scalar(
            select(CreditWallet).where(CreditWallet.account_id == account_id).with_for_update()
        )
        assert wallet is not None
        if reference is not None:
            existing = await s.scalar(
                select(LedgerTransaction.id).where(
                    LedgerTransaction.account_id == account_id,
                    LedgerTransaction.reference == reference,
                )
            )
            if existing is not None:
                return PostingResult(existing, wallet.balance, True)
        new_balance = wallet.balance + customer.amount
        if new_balance < 0 and not allow_negative:
            raise QuotaExceeded(
                "insufficient credits", balance=wallet.balance, required=-customer.amount
            )
        txn = LedgerTransaction(
            id=new_id("txn"),
            account_id=account_id,
            kind=kind.value,
            reference=reference,
            memo=memo,
        )
        s.add(txn)
        await s.flush()
        for p in (customer, system):
            s.add(
                LedgerEntry(
                    id=new_id("led"),
                    transaction_id=txn.id,
                    account_id=account_id,
                    book=p.book,
                    amount=p.amount,
                )
            )
        wallet.balance = new_balance
        if actor is not None:
            await audit.record(
                s,
                actor_type=actor[0],
                actor_id=actor[1],
                account_id=account_id,
                action=f"credits.{kind.value}",
                target_type="ledger_transaction",
                target_id=txn.id,
                after={"amount": customer.amount, "memo": memo},
            )
        return PostingResult(txn.id, new_balance, False)

    async def history(self, account_id: str, *, limit: int = 50) -> list[LedgerLine]:
        async with self._db.tenant(account_id) as s:
            rows = await s.execute(
                select(LedgerTransaction, LedgerEntry.amount)
                .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
                .where(LedgerEntry.book == "customer")
                .order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
                .limit(min(limit, 500))
            )
            return [
                LedgerLine(t.id, t.kind, amt, t.memo, t.reference, t.created_at) for t, amt in rows
            ]
