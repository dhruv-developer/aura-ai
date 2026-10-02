"""Waste detectors. Pure.

Each detector returns per-record savings estimates (USD) for one opportunity kind.
The report counts, for each record, only the single best opportunity (the maximum), so
overlapping detectors never double-count the same dollar.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from aura_providers.domain.catalog import Tier
from aura_providers.domain.pricing import cost_usd
from aura_providers.domain.types import ChatRequest, Message, Usage
from aura_routing import complexity_score, extract_features
from aura_xray.domain.records import PriceBook, TrafficRecord
from aura_xray.domain.workloads import normalize_light

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class OpportunitySpec:
    kind: str
    title: str
    recommendation: str
    confidence: float  # how likely the estimate holds after shadow verification


SPECS = {
    "model_downgrade": OpportunitySpec(
        "model_downgrade",
        "Frontier models on simple requests",
        "Route low-complexity requests to a small model; escalate only when quality checks fail.",
        0.6,
    ),
    "exact_cache": OpportunitySpec(
        "exact_cache",
        "Repeated identical requests",
        "Serve exact repeats from AURA's response cache.",
        0.9,
    ),
    "semantic_cache": OpportunitySpec(
        "semantic_cache",
        "Near-duplicate requests",
        "Enable semantic caching with a verification step for near-identical prompts.",
        0.45,
    ),
    "prompt_caching": OpportunitySpec(
        "prompt_caching",
        "Large system prompts re-sent without caching",
        "Enable provider prompt caching for stable system-prompt prefixes.",
        0.8,
    ),
    "context_trimming": OpportunitySpec(
        "context_trimming",
        "Oversized context for short answers",
        "Compile context: drop irrelevant or duplicated material before the model call.",
        0.4,
    ),
    "truncation_waste": OpportunitySpec(
        "truncation_waste",
        "Responses cut off at the token limit",
        "Raise max_tokens or ask for concise output: truncated answers are usually retried.",
        0.5,
    ),
    "failed_calls": OpportunitySpec(
        "failed_calls",
        "Billed failed calls",
        "Fail over across models with circuit breaking instead of retrying a failing model.",
        0.7,
    ),
}


def _complexity(rec: TrafficRecord) -> float | None:
    if "complexity" in rec.features:
        return float(rec.features["complexity"])
    if not rec.user_content:
        return None
    msgs = (Message("system", rec.system_prompt),) if rec.system_prompt else ()
    req = ChatRequest((*msgs, Message("user", rec.user_content)))
    return complexity_score(
        extract_features(req, est_input_tokens=rec.input_tokens, max_tokens=rec.output_tokens)
    )


def model_downgrade(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    small = prices.cheapest(Tier.SMALL)
    out = [ZERO] * len(records)
    if small is None:
        return out
    for i, r in enumerate(records):
        cost = costs[i]
        if cost is None or r.status != "ok" or prices.tier_of(r.model) is not Tier.FRONTIER:
            continue
        score = _complexity(r)
        if score is None or score >= 0.35:
            continue
        alt = cost_usd(small, Usage(r.input_tokens, r.output_tokens))
        out[i] = max(cost - alt, ZERO)
    return out


def _request_hash(r: TrafficRecord, *, normalized: bool) -> str | None:
    if r.user_content is None:
        return None
    # Light normalization only (case/whitespace/punctuation): ids and numbers change the
    # right answer, so they must still differ for a cache hit.
    norm = normalize_light if normalized else (lambda s: s)
    body = "\x00".join([r.model, norm(r.system_prompt or ""), norm(r.user_content)])
    return hashlib.sha256(body.encode()).hexdigest()


def _repeat_savings(
    records: list[TrafficRecord], costs: list[Decimal | None], *, normalized: bool
) -> list[Decimal]:
    out = [ZERO] * len(records)
    seen: Counter[str] = Counter()
    for i, r in enumerate(records):
        h = _request_hash(r, normalized=normalized)
        if h is None or costs[i] is None or r.status != "ok":
            continue
        if seen[h]:
            out[i] = costs[i] or ZERO  # every repeat after the first could be served from cache
        seen[h] += 1
    return out


def exact_cache(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    return _repeat_savings(records, costs, normalized=False)


def semantic_cache(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    exact = _repeat_savings(records, costs, normalized=False)
    loose = _repeat_savings(records, costs, normalized=True)
    # Only what exact caching would not already catch.
    return [lo if ex == ZERO else ZERO for lo, ex in zip(loose, exact, strict=True)]


def prompt_caching(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    out = [ZERO] * len(records)
    # Provider prompt caching needs a byte-identical prefix, so group on the exact text.
    templates: Counter[str] = Counter(r.system_prompt for r in records if r.system_prompt)
    for i, r in enumerate(records):
        spec = prices.spec(r.model)
        if spec is None or not r.system_prompt or r.cached_input_tokens > 0:
            continue
        sys_tokens = len(r.system_prompt) // 4
        if sys_tokens < 1_024 or templates[r.system_prompt] < 5:
            continue
        per_token = (spec.prices.input - spec.prices.cached_input) / Decimal(1_000_000)
        out[i] = max(sys_tokens * per_token, ZERO)
    return out


def context_trimming(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    out = [ZERO] * len(records)
    for i, r in enumerate(records):
        spec = prices.spec(r.model)
        if spec is None or r.input_tokens < 8_000 or r.output_tokens > 400:
            continue
        # Conservative: assume a context compiler removes 30% of uncached input.
        uncached = r.input_tokens - r.cached_input_tokens
        out[i] = Decimal(uncached) * Decimal("0.3") * spec.prices.input / Decimal(1_000_000)
    return out


def truncation_waste(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    return [
        (costs[i] or ZERO) if r.finish_reason == "length" else ZERO for i, r in enumerate(records)
    ]


def failed_calls(
    records: list[TrafficRecord], costs: list[Decimal | None], prices: PriceBook
) -> list[Decimal]:
    return [
        (costs[i] or ZERO) if r.status == "error" and (r.input_tokens or r.output_tokens) else ZERO
        for i, r in enumerate(records)
    ]


DETECTORS = {
    "model_downgrade": model_downgrade,
    "exact_cache": exact_cache,
    "semantic_cache": semantic_cache,
    "prompt_caching": prompt_caching,
    "context_trimming": context_trimming,
    "truncation_waste": truncation_waste,
    "failed_calls": failed_calls,
}
