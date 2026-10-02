from decimal import Decimal

from aura_policy import ExecutionPolicy, FrontierUsage, QualityMode, merge


def test_limits_merge_stricter_wins_and_cannot_be_loosened() -> None:
    org = ExecutionPolicy(
        max_cost_usd=Decimal("0.05"),
        max_latency_ms=3000,
        allowed_models=frozenset({"a", "b", "c"}),
        frontier_usage=FrontierUsage.ESCALATION_ONLY,
        data_regions=frozenset({"in", "eu"}),
    )
    request = ExecutionPolicy(
        max_cost_usd=Decimal("1.00"),  # tries to loosen: ignored
        max_latency_ms=1000,
        allowed_models=frozenset({"b", "c", "z"}),
        disallowed_models=frozenset({"c"}),
        frontier_usage=FrontierUsage.ALLOWED,  # tries to loosen: ignored
        data_regions=frozenset({"in"}),
    )
    p = merge(org, request)
    assert p.max_cost_usd == Decimal("0.05") and p.max_latency_ms == 1000
    assert p.allowed_models == {"b", "c"} and p.disallowed_models == {"c"}
    assert p.permits_model("b") and not p.permits_model("c") and not p.permits_model("z")
    assert p.effective_frontier is FrontierUsage.ESCALATION_ONLY
    assert p.data_regions == {"in"}


def test_preferences_most_specific_wins_and_defaults() -> None:
    p = merge(
        ExecutionPolicy(quality=QualityMode.BEST, effort="high"),
        None,
        ExecutionPolicy(quality=QualityMode.FAST),
    )
    assert p.quality is QualityMode.FAST and p.effort == "high"
    empty = merge()
    assert empty.effective_quality is QualityMode.AUTO
    assert empty.effective_frontier is FrontierUsage.ALLOWED and empty.permits_model("x")


def test_serialization_roundtrip_and_strictness() -> None:
    import pytest

    from aura_policy import PolicySpecError, from_dict, to_dict

    p = ExecutionPolicy(
        max_cost_usd=Decimal("0.02"),
        max_latency_ms=3000,
        allowed_models=frozenset({"b", "a"}),
        frontier_usage=FrontierUsage.ESCALATION_ONLY,
        data_regions=frozenset({"in"}),
        quality=QualityMode.BALANCED,
        effort="low",
    )
    d = to_dict(p)
    assert d["allowed_models"] == ["a", "b"] and d["frontier_usage"] == "escalation_only"
    assert from_dict(d) == p
    assert from_dict({}) == ExecutionPolicy()
    for bad in [
        {"max_cost": 1},
        {"max_cost_usd": -1},
        {"max_latency_ms": 0},
        {"allowed_models": "a"},
        {"frontier_usage": "sometimes"},
        {"effort": "huge"},
        {"max_output_tokens": True},
    ]:
        with pytest.raises(PolicySpecError):
            from_dict(bad)


def test_cache_settings_merge_stricter_wins() -> None:
    from aura_policy import CacheMode, from_dict, to_dict

    org = ExecutionPolicy(cache_mode=CacheMode.EXACT, cache_ttl_s=3600, cache_similarity=0.95)
    req = ExecutionPolicy(cache_mode=CacheMode.SEMANTIC, cache_ttl_s=60, cache_similarity=0.9)
    p = merge(org, req)
    assert p.cache_mode is CacheMode.EXACT  # a request cannot upgrade to semantic
    assert p.cache_ttl_s == 60 and p.cache_similarity == 0.95
    assert merge(ExecutionPolicy(cache_mode=CacheMode.OFF), req).effective_cache is CacheMode.OFF
    assert merge().effective_cache is CacheMode.OFF  # off by default
    assert from_dict(to_dict(org)) == org


def test_context_mode_strict_wins() -> None:
    from aura_policy import ContextMode, PolicySpecError, from_dict, to_dict

    org = ExecutionPolicy(context_mode=ContextMode.STRICT)
    req = ExecutionPolicy(context_mode=ContextMode.BUDGET, context_token_budget=1000)
    p = merge(org, req)
    assert p.effective_context is ContextMode.STRICT and p.context_token_budget == 1000
    assert merge().effective_context is ContextMode.STRICT  # never alter context by default
    assert from_dict(to_dict(req)) == req
    import pytest

    with pytest.raises(PolicySpecError):
        from_dict({"context_token_budget": 10})
