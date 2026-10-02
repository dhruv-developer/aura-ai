"""Usage periods (UTC calendar day / month). Pure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def period_label(period: str | None, now: datetime) -> str:
    now = now.astimezone(UTC)
    if period == "month":
        return now.strftime("%Y-%m")
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period is None:
        return "all"
    raise ValueError(f"unknown period {period!r}")


def period_end(period: str | None, now: datetime) -> datetime | None:
    now = now.astimezone(UTC)
    if period == "day":
        return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    if period == "month":
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return datetime(year, month, 1, tzinfo=UTC)
    if period is None:
        return None
    raise ValueError(f"unknown period {period!r}")


def counter_expiry_ms(period: str | None, now: datetime, *, grace_days: int = 35) -> int:
    """Counters outlive their period by a grace window so the reconciler can snapshot them."""
    end = period_end(period, now)
    if end is None:
        return 0
    return int(((end - now).total_seconds() + grace_days * 86400) * 1000)
