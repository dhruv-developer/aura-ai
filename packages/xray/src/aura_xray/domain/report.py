"""Build the X-Ray report from parsed records. Pure."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aura_xray.domain.detectors import DETECTORS, SPECS, ZERO
from aura_xray.domain.records import PriceBook, TrafficRecord
from aura_xray.domain.workloads import discover

CENT = Decimal("0.01")
DAYS_PER_MONTH = Decimal(30)
CONTEXT_BANDS = (
    (1_000, "<1k"),
    (8_000, "1k-8k"),
    (32_000, "8k-32k"),
    (128_000, "32k-128k"),
    (float("inf"), ">128k"),
)


def _money(d: Decimal) -> str:
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _window(records: list[TrafficRecord]) -> tuple[datetime | None, datetime | None, Decimal]:
    stamps = [r.ts for r in records if r.ts is not None]
    if len(stamps) < 2:
        return (stamps[0] if stamps else None), (stamps[0] if stamps else None), Decimal(1)
    start, end = min(stamps), max(stamps)
    days = Decimal(max((end - start).total_seconds(), 3600)) / Decimal(86_400)
    return start, end, days


def build_report(records: list[TrafficRecord], prices: PriceBook) -> dict[str, Any]:
    if not records:
        return {"summary": {"requests": 0}, "workloads": [], "opportunities": [], "models": []}
    costs = [prices.cost(r) for r in records]
    priced = [c for c in costs if c is not None]
    spend = sum(priced, ZERO)
    start, end, days = _window(records)
    monthly_factor = DAYS_PER_MONTH / days

    per_kind = {kind: fn(records, costs, prices) for kind, fn in DETECTORS.items()}
    conf = {k: Decimal(str(SPECS[k].confidence)) for k in per_kind}
    # Potential: best opportunity per request at face value.
    # Expected: best confidence-weighted opportunity per request (the headline).
    potential = [max((per_kind[k][i] for k in per_kind), default=ZERO) for i in range(len(records))]
    best = [
        max((per_kind[k][i] * conf[k] for k in per_kind), default=ZERO) for i in range(len(records))
    ]
    addressable = sum(best, ZERO)
    potential_total = sum(potential, ZERO)

    workloads = discover(records)
    wl_of = {i: w for w in workloads for i in w.record_indexes}

    # Opportunities aggregated per (kind, workload); savings shown are kind-specific
    # (they may overlap), while the headline uses the non-overlapping per-record max.
    opp: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, savings in per_kind.items():
        for i, s in enumerate(savings):
            if s <= 0:
                continue
            w = wl_of[i]
            entry = opp.setdefault(
                (kind, w.key),
                {
                    "kind": kind,
                    "workload_key": w.key,
                    "workload": w.label,
                    "requests": 0,
                    "savings": ZERO,
                },
            )
            entry["requests"] += 1
            entry["savings"] += s
    opportunities = []
    for entry in opp.values():
        spec = SPECS[entry["kind"]]
        monthly = entry["savings"] * monthly_factor
        expected = monthly * Decimal(str(spec.confidence))
        if monthly < CENT:
            continue
        opportunities.append(
            {
                "kind": spec.kind,
                "title": spec.title,
                "workload": entry["workload"],
                "workload_key": entry["workload_key"],
                "affected_requests": entry["requests"],
                "potential_monthly_savings_usd": _money(monthly),
                "expected_monthly_savings_usd": _money(expected),
                "confidence": spec.confidence,
                "recommendation": spec.recommendation,
            }
        )
    opportunities.sort(key=lambda o: -Decimal(o["expected_monthly_savings_usd"]))

    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"requests": 0, "spend": ZERO, "in": 0, "out": 0}
    )
    for r, c in zip(records, costs, strict=True):
        m = by_model[r.model]
        m["requests"] += 1
        m["spend"] += c or ZERO
        m["in"] += r.input_tokens
        m["out"] += r.output_tokens
    models = sorted(
        (
            {
                "model": name,
                "requests": v["requests"],
                "share_of_spend": float(v["spend"] / spend) if spend else 0.0,
                "monthly_spend_usd": _money(v["spend"] * monthly_factor),
                "input_tokens": v["in"],
                "output_tokens": v["out"],
                "tier": (t.name.lower() if (t := prices.tier_of(name)) else "unknown"),
            }
            for name, v in by_model.items()
        ),
        key=lambda m: -Decimal(m["monthly_spend_usd"]),
    )

    wl_rows: list[dict[str, Any]] = []
    for w in workloads:
        idx = w.record_indexes
        w_spend = sum((costs[i] or ZERO for i in idx), ZERO)
        w_save = sum((best[i] for i in idx), ZERO)
        inputs = [records[i].input_tokens for i in idx]
        lat = [x for i in idx if (x := records[i].latency_ms) is not None]
        wl_rows.append(
            {
                "key": w.key,
                "label": w.label,
                "requests": len(idx),
                "monthly_spend_usd": _money(w_spend * monthly_factor),
                "est_monthly_savings_usd": _money(w_save * monthly_factor),
                "models": sorted({records[i].model for i in idx}),
                "input_tokens_p50": _pct(inputs, 0.5),
                "input_tokens_p95": _pct(inputs, 0.95),
                "latency_ms_p50": int(statistics.median(lat)) if lat else None,
                "error_rate": round(sum(records[i].status == "error" for i in idx) / len(idx), 4),
            }
        )
    wl_rows.sort(key=lambda w: -Decimal(w["monthly_spend_usd"]))

    inputs = [r.input_tokens for r in records]
    outputs = [r.output_tokens for r in records]
    bands = {name: 0 for _, name in CONTEXT_BANDS}
    for n_tokens in inputs:
        bands[next(name for limit, name in CONTEXT_BANDS if n_tokens < limit)] += 1
    monthly_spend = spend * monthly_factor
    monthly_savings = addressable * monthly_factor
    return {
        "summary": {
            "requests": len(records),
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
            "observed_days": float(days.quantize(Decimal("0.01"))),
            "observed_spend_usd": _money(spend),
            "monthly_spend_usd": _money(monthly_spend),
            "est_monthly_savings_usd": _money(monthly_savings),
            "potential_monthly_savings_usd": _money(potential_total * monthly_factor),
            "est_savings_pct": round(float(monthly_savings / monthly_spend) * 100, 1)
            if monthly_spend
            else 0.0,
            "projected_monthly_spend_usd": _money(monthly_spend - monthly_savings),
            "unpriced_requests": len(records) - len(priced),
            "error_rate": round(sum(r.status == "error" for r in records) / len(records), 4),
            "input_tokens_p50": _pct(inputs, 0.5),
            "input_tokens_p95": _pct(inputs, 0.95),
            "output_tokens_p50": _pct(outputs, 0.5),
            "output_tokens_p95": _pct(outputs, 0.95),
            "context_distribution": bands,
            "methodology": (
                "Savings are estimates from observed traffic. Each request counts only its "
                "single best opportunity, weighted by that opportunity's confidence "
                "(potential = unweighted). Verify with a shadow run before deploying changes."
            ),
        },
        "models": models,
        "workloads": wl_rows,
        "opportunities": opportunities,
    }
