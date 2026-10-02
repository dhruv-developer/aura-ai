"""Subscription state machine. Pure."""

from __future__ import annotations

from enum import StrEnum

from aura_shared.errors import Conflict


class SubscriptionStatus(StrEnum):
    INCOMPLETE = "incomplete"  # checkout started, not yet paid
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    PAUSED = "paused"
    CANCELED = "canceled"


S = SubscriptionStatus
LIVE = frozenset({S.TRIALING, S.ACTIVE, S.PAST_DUE, S.PAUSED})

TRANSITIONS: dict[SubscriptionStatus, frozenset[SubscriptionStatus]] = {
    S.INCOMPLETE: frozenset({S.ACTIVE, S.CANCELED}),
    S.TRIALING: frozenset({S.ACTIVE, S.PAST_DUE, S.CANCELED}),
    S.ACTIVE: frozenset({S.PAST_DUE, S.PAUSED, S.CANCELED}),
    S.PAST_DUE: frozenset({S.ACTIVE, S.CANCELED}),
    S.PAUSED: frozenset({S.ACTIVE, S.CANCELED}),
    S.CANCELED: frozenset(),
}


def can_transition(current: SubscriptionStatus | str, target: SubscriptionStatus | str) -> bool:
    cur, tgt = S(current), S(target)
    return cur == tgt or tgt in TRANSITIONS[cur]


def transition(current: SubscriptionStatus | str, target: SubscriptionStatus | str) -> S:
    if not can_transition(current, target):
        raise Conflict(f"subscription cannot go from {current} to {target}")
    return S(target)
