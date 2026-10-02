"""The single place Redis keys are built.

Layout: ``aura:{env}:{domain}:{{tenant}}:{id...}:v{schema}``

The tenant segment is wrapped in a Redis Cluster hash tag so every key of one tenant
lands in the same slot. That keeps multi-key Lua scripts (quota reserve/commit, which
touch a counter, a hash and a zset) cluster-safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aura_shared.settings import get_settings

GLOBAL_TENANT = "_global"
_FORBIDDEN = frozenset(":{}\n\r ")


@dataclass(frozen=True, slots=True)
class _Family:
    domain: str
    schema: int
    role: str  # which Redis cluster role owns this family: state | cache | vector


class KeyFamily(Enum):
    # --- state (noeviction, persisted) ---
    SESSION = _Family("session", 1, "state")
    SESSION_INDEX = _Family("session_idx", 1, "state")
    ADMIN_SESSION = _Family("admin_session", 1, "state")
    MAGIC_LINK = _Family("magic", 1, "state")
    OAUTH_STATE = _Family("oauth_state", 1, "state")
    THROTTLE = _Family("throttle", 1, "state")
    RATE_LIMIT = _Family("rl", 1, "state")
    QUOTA_USED = _Family("quota", 1, "state")
    QUOTA_RESERVATIONS = _Family("quota_res", 1, "state")
    QUOTA_DEADLINES = _Family("quota_dl", 1, "state")
    CIRCUIT = _Family("cb", 1, "state")
    IDEMPOTENCY = _Family("idem", 1, "state")
    LOCK = _Family("lock", 1, "state")
    WORKING_MEMORY = _Family("wm", 1, "state")
    DEPLOY_STATS = _Family("dstats", 1, "state")
    EXEC_VARIANT = _Family("xvar", 1, "state")
    EXEC_SUMMARY = _Family("xsum", 1, "state")  # shareable, content-free summaries
    LEARNING = _Family("learn", 1, "state")  # bandit state + decision logs
    SESSION_MEMORY = _Family("sm", 1, "state")
    # --- cache (allkeys-lfu, not persisted) ---
    POLICY = _Family("policy", 1, "cache")
    PROMPT = _Family("prompt", 1, "cache")
    ENTITLEMENTS = _Family("ent", 1, "cache")
    CREDIT_BALANCE = _Family("credbal", 1, "cache")  # gate only; Postgres is the truth
    API_KEY = _Family("apikey", 3, "cache")  # v3: + account status
    PROVIDER_CREDENTIAL = _Family("pcred", 1, "cache")  # sealed only, never plaintext
    MODEL_CATALOG = _Family("catalog", 1, "cache")
    PROVIDER_HEALTH = _Family("phealth", 1, "cache")
    RESPONSE_EXACT = _Family("resp", 1, "cache")
    RETRIEVAL = _Family("retr", 1, "cache")
    TOOL_RESULT = _Family("tool", 1, "cache")
    EMBEDDING = _Family("emb", 1, "cache")
    GENERIC = _Family("kv", 1, "cache")
    # --- vector (semantic cache / memory vectors) ---
    RESPONSE_SEMANTIC = _Family("sresp", 1, "vector")
    MEMORY_VECTORS = _Family("memvec", 1, "vector")  # rebuildable from Postgres

    @property
    def domain(self) -> str:
        return self.value.domain

    @property
    def schema(self) -> int:
        return self.value.schema

    @property
    def role(self) -> str:
        return self.value.role


def _check(segment: str, what: str) -> str:
    if not segment or any(ch in _FORBIDDEN for ch in segment):
        raise ValueError(f"invalid redis key {what}: {segment!r}")
    return segment


def key_part(value: str) -> str:
    """Encode an arbitrary identifier (e.g. a model id like ``ollama/gemma:2b``) as a
    single key segment: separators and other forbidden characters are %-escaped, so
    distinct values can never collide or split into extra segments."""
    if not value:
        raise ValueError("empty redis key part")
    return "".join(f"%{ord(ch):02X}" if ch in _FORBIDDEN or ch == "%" else ch for ch in value)


def build_key(family: KeyFamily, tenant: str, *parts: str, env: str | None = None) -> str:
    """Build a namespaced, tenant-tagged, schema-versioned key.

    ``tenant`` must be an account id, or :data:`GLOBAL_TENANT` for platform-wide data.
    """
    env_name = env or get_settings().env.value
    segs = [_check(p, "part") for p in parts]
    body = ":".join([_check(env_name, "env"), family.domain, "{" + _check(tenant, "tenant") + "}"])
    tail = (":" + ":".join(segs)) if segs else ""
    return f"aura:{body}{tail}:v{family.schema}"
