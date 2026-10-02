"""Double-entry credit postings. Pure.

Credits are integers (1 credit = the smallest billable unit; pricing maps credits to
money elsewhere). The ``customer`` book is the account's spendable balance. Every
transaction has balancing entries in a system book, so the sum is always zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aura_shared.errors import ValidationFailed


class TxnKind(StrEnum):
    GRANT = "grant"  # free/plan/referral credits
    PURCHASE = "purchase"  # paid top-up
    CONSUME = "consume"  # usage
    REFUND = "refund"  # give back consumed credits
    EXPIRE = "expire"  # remove unused expiring credits
    ADJUST = "adjust"  # manual correction (either sign), superadmin only


@dataclass(frozen=True, slots=True)
class Posting:
    book: str
    amount: int


_COUNTER_BOOK = {
    TxnKind.GRANT: "system_grants",
    TxnKind.PURCHASE: "system_sales",
    TxnKind.CONSUME: "system_usage",
    TxnKind.REFUND: "system_usage",
    TxnKind.EXPIRE: "system_expiry",
    TxnKind.ADJUST: "system_adjust",
}
_CUSTOMER_SIGN = {
    TxnKind.GRANT: 1,
    TxnKind.PURCHASE: 1,
    TxnKind.CONSUME: -1,
    TxnKind.REFUND: 1,
    TxnKind.EXPIRE: -1,
    TxnKind.ADJUST: 1,
}


def postings_for(kind: TxnKind, amount: int) -> tuple[Posting, Posting]:
    """Return balanced (customer, system) postings. ``amount`` is positive, except that
    ADJUST may be negative."""
    if amount == 0 or (kind is not TxnKind.ADJUST and amount < 0):
        raise ValidationFailed("amount must be positive (adjustments may be negative)")
    delta = _CUSTOMER_SIGN[kind] * amount
    return Posting("customer", delta), Posting(_COUNTER_BOOK[kind], -delta)


def customer_delta(kind: TxnKind, amount: int) -> int:
    return postings_for(kind, amount)[0].amount
