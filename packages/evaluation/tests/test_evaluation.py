from decimal import Decimal

import pytest

from aura_evaluation import (
    JudgeVerdict,
    bootstrap_ci,
    compare,
    evaluate_sample,
    gate,
    is_json,
    rule_check,
    summarize,
    token_f1,
    wilson_interval,
)


def test_wilson_interval_properties() -> None:
    lo, hi = wilson_interval(95, 100)
    assert 0.88 < lo < 0.95 < hi < 0.99
    assert wilson_interval(0, 0) == (0.0, 1.0)
    assert wilson_interval(10, 10)[1] == 1.0 and wilson_interval(10, 10)[0] > 0.6
    # Narrows with more data.
    assert (wilson_interval(950, 1000)[1] - wilson_interval(950, 1000)[0]) < (hi - lo)


def test_bootstrap_is_deterministic_and_contains_mean() -> None:
    values = [float(i % 7) for i in range(200)]
    a, b = bootstrap_ci(values), bootstrap_ci(values)
    mean = sum(values) / len(values)
    assert a == b and a[0] < mean < a[1]


def test_metrics() -> None:
    assert token_f1("The capital is Paris.", "Paris is the capital") > 0.8
    assert token_f1("no", "completely different") == 0.0
    assert is_json('{"a": 1}') and is_json('```json\n{"a": 1}\n```') and not is_json("{a:1}")
    assert rule_check("", "x") == (False, "empty output")
    assert rule_check("not json", '{"ok": true}')[0] is False
    assert rule_check("fine", "also fine") == (True, None)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("B", "A", JudgeVerdict.CANDIDATE_BETTER),  # consistent: candidate wins both
        ("A", "B", JudgeVerdict.BASELINE_BETTER),
        ("A", "A", JudgeVerdict.TIE),  # position-biased judge → tie
        ("TIE", "TIE", JudgeVerdict.TIE),
        ("??", "A", JudgeVerdict.UNKNOWN),
    ],
)
async def test_position_swapped_judging(first: str, second: str, expected: JudgeVerdict) -> None:
    answers = iter([first, second])

    async def judge_model(prompt: str) -> str:
        return next(answers)

    assert await compare(judge_model, "q", "baseline", "candidate") == expected


def _ev(not_worse: bool, base: str = "1.0", cand: str = "0.2"):  # type: ignore[no-untyped-def]
    verdict = JudgeVerdict.TIE if not_worse else JudgeVerdict.BASELINE_BETTER
    return evaluate_sample(
        baseline="Paris is the capital",
        candidate="Paris is the capital",
        verdict=verdict,
        baseline_cost=Decimal(base),
        candidate_cost=Decimal(cand),
        baseline_latency_ms=1000,
        candidate_latency_ms=400,
    )


def test_summary_and_gate() -> None:
    good = summarize("small", [_ev(True)] * 200)
    assert good.non_inferiority_rate == 1.0 and good.cost_change_pct == -80.0
    assert gate(good).deploy
    few = summarize("small", [_ev(True)] * 10)
    assert not gate(few).deploy and "coverage" in gate(few).reasons[0]
    worse = summarize("small", [_ev(True)] * 150 + [_ev(False)] * 50)
    decision = gate(worse)
    assert not decision.deploy and any("quality lower bound" in r for r in decision.reasons)
    pricier = summarize("big", [_ev(True, "1.0", "1.5")] * 100)
    assert not gate(pricier).deploy


def test_similarity_proxy_when_judge_unavailable() -> None:
    e = evaluate_sample(
        baseline="the answer is 42",
        candidate="the answer is 42",
        verdict=JudgeVerdict.UNKNOWN,
        baseline_cost=Decimal(1),
        candidate_cost=Decimal(1),
        baseline_latency_ms=None,
        candidate_latency_ms=None,
    )
    assert e.not_worse and e.method == "similarity"
    bad = evaluate_sample(
        baseline='{"a": 1}',
        candidate="oops",
        verdict=JudgeVerdict.CANDIDATE_BETTER,
        baseline_cost=Decimal(1),
        candidate_cost=Decimal(1),
        baseline_latency_ms=None,
        candidate_latency_ms=None,
    )
    assert not bad.not_worse and bad.method == "rules"  # rules override the judge
