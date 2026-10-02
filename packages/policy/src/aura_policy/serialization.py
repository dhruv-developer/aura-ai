"""ExecutionPolicy <-> JSON (stored in policy_versions.spec). Pure and strict: unknown
fields are rejected so a typo never silently loosens a policy."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from aura_policy.policy import (
    CacheMode,
    ContextMode,
    ExecutionPolicy,
    FrontierUsage,
    PiiMode,
    QualityMode,
)

FIELDS = {
    "max_cost_usd",
    "max_latency_ms",
    "max_output_tokens",
    "allowed_models",
    "disallowed_models",
    "frontier_usage",
    "data_regions",
    "quality",
    "effort",
    "cache_mode",
    "cache_ttl_s",
    "cache_similarity",
    "context_mode",
    "context_token_budget",
    "pii",
}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class PolicySpecError(ValueError):
    pass


def to_dict(p: ExecutionPolicy) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if p.max_cost_usd is not None:
        out["max_cost_usd"] = str(p.max_cost_usd)
    if p.max_latency_ms is not None:
        out["max_latency_ms"] = p.max_latency_ms
    if p.max_output_tokens is not None:
        out["max_output_tokens"] = p.max_output_tokens
    if p.allowed_models is not None:
        out["allowed_models"] = sorted(p.allowed_models)
    if p.disallowed_models:
        out["disallowed_models"] = sorted(p.disallowed_models)
    if p.frontier_usage is not None:
        out["frontier_usage"] = p.frontier_usage.name.lower()
    if p.data_regions is not None:
        out["data_regions"] = sorted(p.data_regions)
    if p.quality is not None:
        out["quality"] = p.quality.value
    if p.effort is not None:
        out["effort"] = p.effort
    if p.cache_mode is not None:
        out["cache_mode"] = p.cache_mode.name.lower()
    if p.cache_ttl_s is not None:
        out["cache_ttl_s"] = p.cache_ttl_s
    if p.cache_similarity is not None:
        out["cache_similarity"] = p.cache_similarity
    if p.context_mode is not None:
        out["context_mode"] = p.context_mode.name.lower()
    if p.context_token_budget is not None:
        out["context_token_budget"] = p.context_token_budget
    if p.pii is not None:
        out["pii"] = p.pii.name.lower()
    return out


def _pos_int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise PolicySpecError(f"{name} must be a positive integer")
    return v


def _str_set(v: Any, name: str) -> frozenset[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise PolicySpecError(f"{name} must be a list of non-empty strings")
    return frozenset(v)


def from_dict(d: dict[str, Any]) -> ExecutionPolicy:
    if unknown := set(d) - FIELDS:
        raise PolicySpecError(f"unknown policy fields: {', '.join(sorted(unknown))}")
    kw: dict[str, Any] = {}
    if "max_cost_usd" in d:
        try:
            cost = Decimal(str(d["max_cost_usd"]))
        except InvalidOperation as exc:
            raise PolicySpecError("max_cost_usd must be a number") from exc
        if cost <= 0:
            raise PolicySpecError("max_cost_usd must be positive")
        kw["max_cost_usd"] = cost
    if "max_latency_ms" in d:
        kw["max_latency_ms"] = _pos_int(d["max_latency_ms"], "max_latency_ms")
    if "max_output_tokens" in d:
        kw["max_output_tokens"] = _pos_int(d["max_output_tokens"], "max_output_tokens")
    if "allowed_models" in d:
        kw["allowed_models"] = _str_set(d["allowed_models"], "allowed_models")
    if "disallowed_models" in d:
        kw["disallowed_models"] = _str_set(d["disallowed_models"], "disallowed_models")
    if "frontier_usage" in d:
        try:
            kw["frontier_usage"] = FrontierUsage[str(d["frontier_usage"]).upper()]
        except KeyError as exc:
            raise PolicySpecError("frontier_usage must be never|escalation_only|allowed") from exc
    if "data_regions" in d:
        kw["data_regions"] = _str_set(d["data_regions"], "data_regions")
    if "quality" in d:
        try:
            kw["quality"] = QualityMode(d["quality"])
        except ValueError as exc:
            raise PolicySpecError("quality must be auto|fast|balanced|best") from exc
    if "effort" in d:
        if d["effort"] not in EFFORTS:
            raise PolicySpecError(f"effort must be one of {sorted(EFFORTS)}")
        kw["effort"] = d["effort"]
    if "cache_mode" in d:
        try:
            kw["cache_mode"] = CacheMode[str(d["cache_mode"]).upper()]
        except KeyError as exc:
            raise PolicySpecError("cache_mode must be off|exact|semantic") from exc
    if "cache_ttl_s" in d:
        kw["cache_ttl_s"] = _pos_int(d["cache_ttl_s"], "cache_ttl_s")
    if "cache_similarity" in d:
        v = d["cache_similarity"]
        if not isinstance(v, int | float) or isinstance(v, bool) or not 0.8 <= v <= 1.0:
            raise PolicySpecError("cache_similarity must be between 0.8 and 1.0")
        kw["cache_similarity"] = float(v)
    if "context_mode" in d:
        try:
            kw["context_mode"] = ContextMode[str(d["context_mode"]).upper()]
        except KeyError as exc:
            raise PolicySpecError("context_mode must be strict|auto|budget") from exc
    if "context_token_budget" in d:
        budget = _pos_int(d["context_token_budget"], "context_token_budget")
        if budget < 256:
            raise PolicySpecError("context_token_budget must be at least 256")
        kw["context_token_budget"] = budget
    if "pii" in d:
        try:
            kw["pii"] = PiiMode[str(d["pii"]).upper()]
        except KeyError as exc:
            raise PolicySpecError("pii must be allow|redact|block") from exc
    return ExecutionPolicy(**kw)
