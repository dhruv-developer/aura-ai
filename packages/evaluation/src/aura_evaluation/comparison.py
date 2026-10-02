"""Candidate vs baseline: per-sample evaluation, aggregation and the deploy gate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aura_evaluation.judge import JudgeVerdict
from aura_evaluation.metrics import rule_check, token_f1
from aura_evaluation.stats import bootstrap_ci, wilson_interval

# Without a usable judge verdict, a candidate counts as "not worse" when its overlap
# with the baseline is at least this high. This proxy is labelled in the results.
SIMILARITY_PROXY_THRESHOLD = 0.6


@dataclass(frozen=True, slots=True)
class SampleEvaluation:
    rules_ok: bool
    rule_failure: str | None
    similarity: float
    verdict: JudgeVerdict
    not_worse: bool  # final decision for this sample
    method: str  # judge | similarity
    baseline_cost: Decimal
    candidate_cost: Decimal
    baseline_latency_ms: int | None
    candidate_latency_ms: int | None


def evaluate_sample(
    *,
    baseline: str,
    candidate: str,
    verdict: JudgeVerdict,
    baseline_cost: Decimal,
    candidate_cost: Decimal,
    baseline_latency_ms: int | None,
    candidate_latency_ms: int | None,
) -> SampleEvaluation:
    ok, failure = rule_check(candidate, baseline)
    sim = token_f1(candidate, baseline)
    judged = verdict.candidate_not_worse
    if not ok:
        not_worse, method = False, "rules"
    elif judged is not None:
        not_worse, method = judged, "judge"
    else:
        not_worse, method = sim >= SIMILARITY_PROXY_THRESHOLD, "similarity"
    return SampleEvaluation(
        ok,
        failure,
        sim,
        verdict,
        not_worse,
        method,
        baseline_cost,
        candidate_cost,
        baseline_latency_ms,
        candidate_latency_ms,
    )


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    model: str
    samples: int
    non_inferiority_rate: float
    non_inferiority_ci: tuple[float, float]
    mean_similarity: float
    rule_failures: int
    judge_coverage: float  # share of samples decided by the judge
    baseline_cost_usd: Decimal
    candidate_cost_usd: Decimal
    cost_change_pct: float
    cost_change_ci_pct: tuple[float, float]
    latency_p50_baseline_ms: int | None
    latency_p50_candidate_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "samples": self.samples,
            "non_inferiority_rate": round(self.non_inferiority_rate, 4),
            "non_inferiority_ci": [round(x, 4) for x in self.non_inferiority_ci],
            "mean_similarity": round(self.mean_similarity, 4),
            "rule_failures": self.rule_failures,
            "judge_coverage": round(self.judge_coverage, 4),
            "baseline_cost_usd": str(self.baseline_cost_usd),
            "candidate_cost_usd": str(self.candidate_cost_usd),
            "cost_change_pct": round(self.cost_change_pct, 2),
            "cost_change_ci_pct": [round(x, 2) for x in self.cost_change_ci_pct],
            "latency_p50_baseline_ms": self.latency_p50_baseline_ms,
            "latency_p50_candidate_ms": self.latency_p50_candidate_ms,
        }


def _p50(values: list[int]) -> int | None:
    return sorted(values)[len(values) // 2] if values else None


def summarize(model: str, evals: list[SampleEvaluation]) -> CandidateSummary:
    n = len(evals)
    wins = sum(e.not_worse for e in evals)
    base = sum((e.baseline_cost for e in evals), Decimal(0))
    cand = sum((e.candidate_cost for e in evals), Decimal(0))
    change = float((cand - base) / base * 100) if base else 0.0
    # Per-sample relative cost change, bootstrapped (paired design).
    ratios = [
        float((e.candidate_cost - e.baseline_cost) / e.baseline_cost * 100)
        for e in evals
        if e.baseline_cost > 0
    ]
    return CandidateSummary(
        model=model,
        samples=n,
        non_inferiority_rate=wins / n if n else 0.0,
        non_inferiority_ci=wilson_interval(wins, n),
        mean_similarity=sum(e.similarity for e in evals) / n if n else 0.0,
        rule_failures=sum(not e.rules_ok for e in evals),
        judge_coverage=sum(e.method == "judge" for e in evals) / n if n else 0.0,
        baseline_cost_usd=base,
        candidate_cost_usd=cand,
        cost_change_pct=change,
        cost_change_ci_pct=bootstrap_ci(ratios) if ratios else (0.0, 0.0),
        latency_p50_baseline_ms=_p50(
            [e.baseline_latency_ms for e in evals if e.baseline_latency_ms is not None]
        ),
        latency_p50_candidate_ms=_p50(
            [e.candidate_latency_ms for e in evals if e.candidate_latency_ms is not None]
        ),
    )


@dataclass(frozen=True, slots=True)
class GateDecision:
    deploy: bool
    reasons: list[str]


def gate(
    s: CandidateSummary,
    *,
    quality_target: float = 0.9,
    min_samples: int = 30,
    min_savings_pct: float = 5.0,
) -> GateDecision:
    """Refuse a deployment unless it is clearly safe (spec §153): enough evaluation
    coverage, the *lower* confidence bound meets the quality target, no hard rule
    failures, and a material saving."""
    reasons = []
    if s.samples < min_samples:
        reasons.append(f"insufficient evaluation coverage ({s.samples} < {min_samples} samples)")
    if s.non_inferiority_ci[0] < quality_target:
        reasons.append(
            f"quality lower bound {s.non_inferiority_ci[0]:.2f} below target {quality_target:.2f}"
        )
    if s.rule_failures:
        reasons.append(f"{s.rule_failures} hard rule failures")
    if -s.cost_change_pct < min_savings_pct:
        reasons.append(f"savings {-s.cost_change_pct:.1f}% below {min_savings_pct:.0f}% threshold")
    return GateDecision(deploy=not reasons, reasons=reasons or ["all gates passed"])
