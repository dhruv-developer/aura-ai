from decimal import Decimal

import pytest

from aura_policy import ExecutionPolicy, FrontierUsage, QualityMode
from aura_providers.domain.catalog import Tier, load_catalog
from aura_providers.domain.tokens import estimate_input_tokens
from aura_providers.domain.types import ChatRequest, Message
from aura_routing import NoEligibleModel, Router, complexity_score, extract_features
from aura_shared.errors import Forbidden, ValidationFailed

CAT = load_catalog(enabled_providers={"anthropic"})
R = Router(CAT)


def _req(text: str) -> ChatRequest:
    return ChatRequest((Message("user", text),))


def route(text: str, policy: ExecutionPolicy | None = None, **kw):  # type: ignore[no-untyped-def]
    req = _req(text)
    kw.setdefault("requested_max_tokens", 1000)
    return R.route(
        req, policy or ExecutionPolicy(), est_input_tokens=estimate_input_tokens(req), **kw
    )


def test_simple_prompt_goes_small_complex_goes_frontier() -> None:
    assert route("What is the capital of France?").primary.id == "anthropic/claude-haiku-4-5"
    hard = (
        "Prove the theorem step by step, analyze the trade-offs and design an "
        "architecture:\n```python\ndef f(x):\n    return x ** 2\n```\n\\frac{a}{b}"
    )
    r = route(hard)
    assert r.primary.tier is Tier.FRONTIER and "complexity" in r.decision.reason
    assert r.decision.features["has_code"] and r.decision.features["has_math"]
    assert r.decision.candidates[0] == r.primary.id and len(r.fallbacks) == 2


def test_quality_modes_override_auto() -> None:
    assert route("hi", ExecutionPolicy(quality=QualityMode.BEST)).primary.tier is Tier.FRONTIER
    assert route("hi", ExecutionPolicy(quality=QualityMode.BALANCED)).primary.tier is Tier.MEDIUM


def test_frontier_escalation_only_and_never() -> None:
    r = route(
        "hi",
        ExecutionPolicy(quality=QualityMode.BEST, frontier_usage=FrontierUsage.ESCALATION_ONLY),
    )
    assert r.primary.tier is Tier.MEDIUM
    assert any(f.tier is Tier.FRONTIER for f in r.fallbacks)  # escalation still possible
    never = route(
        "hi", ExecutionPolicy(quality=QualityMode.BEST, frontier_usage=FrontierUsage.NEVER)
    )
    assert all(s.tier is not Tier.FRONTIER for s in never.chain)


def test_open_circuits_are_routed_around() -> None:
    r = route("hi", open_circuits=frozenset({"anthropic/claude-haiku-4-5"}))
    assert r.primary.id != "anthropic/claude-haiku-4-5"
    assert "anthropic/claude-haiku-4-5" not in r.decision.candidates


def test_budget_clamps_output_and_excludes_unaffordable() -> None:
    # $0.001 buys ~180 output tokens on Haiku but too few on Opus.
    r = route(
        "hi",
        ExecutionPolicy(max_cost_usd=Decimal("0.001"), quality=QualityMode.BEST),
        requested_max_tokens=4000,
    )
    assert r.primary.id == "anthropic/claude-sonnet-5-5" or r.primary.tier is not Tier.FRONTIER
    assert all(r.max_tokens[s.id] < 4000 for s in r.chain)
    with pytest.raises(NoEligibleModel):
        route("hi", ExecutionPolicy(max_cost_usd=Decimal("0.0000001")))


def test_policy_allow_list_region_and_context_window() -> None:
    r = route("hi", ExecutionPolicy(allowed_models=frozenset({"anthropic/claude-opus-5-5"})))
    assert [s.id for s in r.chain] == ["anthropic/claude-opus-5-5"]
    with pytest.raises(NoEligibleModel):  # global models do not satisfy India-only residency
        route("hi", ExecutionPolicy(data_regions=frozenset({"in"})))
    big = "x " * 500_000  # ~250k tokens: beyond Haiku's 200k window
    assert route(big).primary.id != "anthropic/claude-haiku-4-5"


def test_explicit_model_never_substituted() -> None:
    r = route("hi", explicit_model="claude-opus-5-5")
    assert r.primary.id == "anthropic/claude-opus-5-5" and r.fallbacks == ()
    with pytest.raises(ValidationFailed):
        route("hi", explicit_model="gpt-imaginary")
    with pytest.raises(Forbidden):
        route(
            "hi",
            ExecutionPolicy(disallowed_models=frozenset({"anthropic/claude-opus-5-5"})),
            explicit_model="claude-opus-5-5",
        )


def test_complexity_score_is_bounded_and_monotonic() -> None:
    simple = extract_features(_req("hi"), est_input_tokens=5, max_tokens=100)
    rich = extract_features(
        _req("analyze and compare, step by step, then design it"),
        est_input_tokens=20_000,
        max_tokens=8000,
    )
    assert 0 <= complexity_score(simple) < complexity_score(rich) <= 1
