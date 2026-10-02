"""TTL registry. Every cache key family has a TTL; state families may be persistent."""

from __future__ import annotations

import random

from aura_shared.redis.keys import KeyFamily

# Seconds. ``None`` means the key is explicit state managed by its owner (e.g. quota
# counters expire at period end, set by the script caller).
_TTL: dict[KeyFamily, int | None] = {
    KeyFamily.SESSION: 60 * 60 * 24 * 14,
    KeyFamily.SESSION_INDEX: 60 * 60 * 24 * 14,
    KeyFamily.ADMIN_SESSION: 60 * 60 * 8,
    KeyFamily.MAGIC_LINK: 60 * 15,
    KeyFamily.OAUTH_STATE: 60 * 10,
    KeyFamily.THROTTLE: None,  # caller supplies the window
    KeyFamily.RATE_LIMIT: None,  # GCRA sets its own PX
    KeyFamily.QUOTA_USED: None,
    KeyFamily.QUOTA_RESERVATIONS: None,
    KeyFamily.QUOTA_DEADLINES: None,
    KeyFamily.CIRCUIT: 60 * 10,
    KeyFamily.IDEMPOTENCY: 60 * 60 * 24,
    KeyFamily.LOCK: None,  # leases carry their own PX
    KeyFamily.WORKING_MEMORY: 60 * 60,
    KeyFamily.DEPLOY_STATS: 60 * 60 * 24 * 30,
    KeyFamily.EXEC_VARIANT: 60 * 60 * 24 * 7,
    KeyFamily.EXEC_SUMMARY: 60 * 60 * 24 * 7,
    KeyFamily.LEARNING: None,  # durable learner state
    KeyFamily.SESSION_MEMORY: 60 * 60 * 24,
    KeyFamily.POLICY: 60 * 5,
    KeyFamily.PROMPT: 60 * 5,
    KeyFamily.ENTITLEMENTS: 60 * 5,
    KeyFamily.CREDIT_BALANCE: 30,
    KeyFamily.API_KEY: 60,
    KeyFamily.PROVIDER_CREDENTIAL: 60,  # short: bounds revocation lag even if invalidation is lost
    KeyFamily.MODEL_CATALOG: 60 * 15,
    KeyFamily.PROVIDER_HEALTH: 60,
    KeyFamily.RESPONSE_EXACT: 60 * 60 * 24,
    KeyFamily.RETRIEVAL: 60 * 30,
    KeyFamily.TOOL_RESULT: 60 * 10,
    KeyFamily.EMBEDDING: 60 * 60 * 24 * 7,
    KeyFamily.GENERIC: 60 * 5,
    KeyFamily.RESPONSE_SEMANTIC: 60 * 60 * 24,
    KeyFamily.MEMORY_VECTORS: None,  # persistent index, mirrors Postgres
}

JITTER_RATIO = 0.10


def ttl_for(
    family: KeyFamily, *, jitter: bool = True, rng: random.Random | None = None
) -> int | None:
    """Return the TTL for a family with ±10% jitter to avoid synchronized expiry."""
    base = _TTL[family]
    if base is None or not jitter:
        return base
    r = rng or random
    delta = int(base * JITTER_RATIO)
    return max(1, base + r.randint(-delta, delta))
