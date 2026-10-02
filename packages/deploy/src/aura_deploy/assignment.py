"""Deterministic, sticky canary assignment. Pure.

A request lands in bucket ``hash(deployment_id, sticky_key) % 10_000``. Growing the
percentage only ever *adds* buckets to the canary, so a user in the canary at 5% stays
in it at 25%. Nobody flips between variants as stages advance.
"""

from __future__ import annotations

import hashlib

BUCKETS = 10_000


def bucket(deployment_id: str, sticky_key: str) -> int:
    digest = hashlib.sha256(f"{deployment_id}:{sticky_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def variant(deployment_id: str, sticky_key: str, percent: int) -> str:
    return "canary" if bucket(deployment_id, sticky_key) < percent * (BUCKETS // 100) else "stable"
