"""Constraint-aware tier router (rules-v1). Pure."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aura_policy import ExecutionPolicy, FrontierUsage, QualityMode
from aura_providers.domain.catalog import ModelCatalog, ModelSpec, Tier
from aura_providers.domain.pricing import MTOK
from aura_providers.domain.types import ChatRequest
from aura_routing.features import Features, complexity_score, extract_features
from aura_shared.errors import Forbidden, ValidationFailed
from aura_shared.events import RoutingDecision

ROUTER_VERSION = "rules-v1"
MIN_USEFUL_OUTPUT = 64
MAX_FALLBACKS = 3
QUALITY_TIER = {
    QualityMode.FAST: Tier.SMALL,
    QualityMode.BALANCED: Tier.MEDIUM,
    QualityMode.BEST: Tier.FRONTIER,
}


class NoEligibleModel(ValidationFailed):
    code = "no_eligible_model"
    title = "No model satisfies the request constraints"


@dataclass(frozen=True, slots=True)
class Route:
    primary: ModelSpec
    fallbacks: tuple[ModelSpec, ...]
    max_tokens: dict[str, int]  # per model: requested output clamped to budget/limits
    decision: RoutingDecision
    target_tier: Tier
    est_input_tokens: int

    @property
    def chain(self) -> tuple[ModelSpec, ...]:
        return (self.primary, *self.fallbacks)


def _blended_price(spec: ModelSpec) -> Decimal:
    # Typical chat traffic is input-heavy; weight 3:1 input:output.
    return spec.prices.input * 3 + spec.prices.output


def _affordable_output(spec: ModelSpec, est_in: int, budget: Decimal | None) -> int | None:
    """Max output tokens the per-request budget allows on ``spec`` (None = no budget)."""
    if budget is None:
        return None
    in_cost = est_in * spec.prices.input / MTOK
    remaining = budget - in_cost
    if remaining <= 0:
        return 0
    if spec.prices.output == 0:
        return spec.max_output
    return int(remaining * MTOK / spec.prices.output)


class Router:
    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog

    def route(
        self,
        req: ChatRequest,
        policy: ExecutionPolicy,
        *,
        est_input_tokens: int,
        requested_max_tokens: int,
        explicit_model: str | None = None,
        open_circuits: frozenset[str] = frozenset(),
        exclude: frozenset[str] = frozenset(),
        region: str = "global",
    ) -> Route:
        features = extract_features(
            req, est_input_tokens=est_input_tokens, max_tokens=requested_max_tokens
        )
        eligible: list[ModelSpec] = []
        max_tokens: dict[str, int] = {}
        for spec in self._catalog.enabled():
            if spec.id in exclude or not policy.permits_model(spec.id):
                continue
            # Residency is strict: a "global" model does not satisfy an India-only policy.
            if policy.data_regions is not None and not set(spec.regions) & policy.data_regions:
                continue
            if policy.effective_frontier is FrontierUsage.NEVER and spec.tier is Tier.FRONTIER:
                continue
            out = min(requested_max_tokens, spec.max_output)
            if policy.max_output_tokens is not None:
                out = min(out, policy.max_output_tokens)
            affordable = _affordable_output(spec, est_input_tokens, policy.max_cost_usd)
            if affordable is not None:
                out = min(out, affordable)
            if out < min(MIN_USEFUL_OUTPUT, requested_max_tokens):
                continue  # budget cannot buy a useful answer on this model
            if est_input_tokens + out > spec.context_window:
                continue
            eligible.append(spec)
            max_tokens[spec.id] = out

        if explicit_model is not None:
            return self._explicit(explicit_model, eligible, max_tokens, features, est_input_tokens)

        healthy = [s for s in eligible if s.id not in open_circuits]
        pool = healthy or eligible  # all circuits open: try anyway rather than fail closed
        if not pool:
            raise NoEligibleModel(
                "no model satisfies the policy, budget and context-window constraints"
            )

        quality = policy.effective_quality
        if quality is QualityMode.AUTO:
            score = complexity_score(features)
            target = Tier.SMALL if score < 0.35 else Tier.MEDIUM if score < 0.65 else Tier.FRONTIER
            reason = f"auto: complexity {score}"
            features["complexity"] = score
        else:
            target = QUALITY_TIER[quality]
            reason = f"quality={quality.value}"
        if policy.effective_frontier is FrontierUsage.ESCALATION_ONLY and target is Tier.FRONTIER:
            target = Tier.MEDIUM
            reason += "; frontier is escalation-only"

        def rank(spec: ModelSpec) -> tuple[int, int, Decimal]:
            distance = int(spec.tier) - int(target)
            # Prefer the target tier, then higher tiers (quality), then lower; cheapest first.
            return (abs(distance), 0 if distance > 0 else 1, _blended_price(spec))

        primaries = [
            s
            for s in pool
            if not (
                policy.effective_frontier is FrontierUsage.ESCALATION_ONLY
                and s.tier is Tier.FRONTIER
            )
        ] or pool
        primary = min(primaries, key=rank)
        fallbacks = tuple(sorted((s for s in pool if s.id != primary.id), key=rank))[:MAX_FALLBACKS]
        if primary.tier != target:
            reason += (
                f"; no eligible {target.name.lower()} model, using {primary.tier.name.lower()}"
            )
        decision = RoutingDecision(
            router_version=ROUTER_VERSION,
            chosen_model=primary.id,
            candidates=[s.id for s in (primary, *fallbacks)],
            features=features,
            propensity=1.0,  # deterministic policy; bandits (V4) log real propensities
            reason=reason,
        )
        return Route(primary, fallbacks, max_tokens, decision, target, est_input_tokens)

    def _explicit(
        self,
        name: str,
        eligible: list[ModelSpec],
        max_tokens: dict[str, int],
        features: Features,
        est_in: int,
    ) -> Route:
        spec = self._catalog.resolve(name)
        if spec is None or not spec.enabled:
            raise ValidationFailed(f"unknown model {name!r}")
        if spec not in eligible:
            raise Forbidden(
                f"model {spec.id!r} is not allowed by policy or cannot fit the request "
                "within its budget and context window"
            )
        decision = RoutingDecision(
            router_version=ROUTER_VERSION,
            chosen_model=spec.id,
            candidates=[spec.id],
            features=features,
            propensity=1.0,
            reason="explicit model requested",
        )
        # Explicit requests are never substituted with another model.
        return Route(spec, (), max_tokens, decision, spec.tier, est_in)
