from decimal import Decimal
from pathlib import Path

import pytest

from aura_providers.domain.catalog import load_catalog
from aura_shared.errors import ValidationFailed
from aura_xray.domain.parsers import parse
from aura_xray.domain.records import PriceBook, TrafficRecord
from aura_xray.domain.report import build_report
from aura_xray.domain.workloads import discover, normalize, normalize_light

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "xray_sample.jsonl"
PRICES = PriceBook(load_catalog())


@pytest.fixture(scope="module")
def report() -> dict:  # type: ignore[type-arg]
    parsed = parse(FIXTURE.read_bytes(), "jsonl")
    assert len(parsed.records) == 2000 and parsed.skipped == 0
    return build_report(parsed.records, PRICES)


def _opp(report, kind, workload_word):  # type: ignore[no-untyped-def]
    return [
        o for o in report["opportunities"] if o["kind"] == kind and workload_word in o["workload"]
    ]


def test_fixture_workloads_are_discovered_as_four(report) -> None:  # type: ignore[no-untyped-def]
    labels = [w["label"] for w in report["workloads"]]
    assert len(labels) == 4, labels  # the name-varying support template merges into one
    assert any("support" in label for label in labels)


def test_planted_waste_is_found(report) -> None:  # type: ignore[no-untyped-def]
    assert _opp(report, "model_downgrade", "support")  # Opus on FAQs
    assert not _opp(report, "model_downgrade", "engineer")  # complex code review stays frontier
    assert _opp(report, "prompt_caching", "legal")  # identical 1.5k-token system prompt
    assert _opp(report, "context_trimming", "legal")  # 12k+ inputs, short answers
    assert _opp(report, "truncation_waste", "engineer")
    assert not _opp(report, "exact_cache", "support")  # per-customer prompts: unsafe to cache
    sentiment = next(w for w in report["workloads"] if "sentiment" in w["label"])
    assert sentiment["est_monthly_savings_usd"] == "0.00"  # already efficient


def test_headline_is_confidence_weighted_and_consistent(report) -> None:  # type: ignore[no-untyped-def]
    s = report["summary"]
    spend, expected = Decimal(s["monthly_spend_usd"]), Decimal(s["est_monthly_savings_usd"])
    potential = Decimal(s["potential_monthly_savings_usd"])
    assert 0 < expected < potential < spend
    assert Decimal(s["projected_monthly_spend_usd"]) == spend - expected
    assert 10 < s["est_savings_pct"] < 40
    per_workload = sum(Decimal(w["est_monthly_savings_usd"]) for w in report["workloads"])
    assert abs(per_workload - expected) <= Decimal("0.05")  # rounding only
    for o in report["opportunities"]:
        assert Decimal(o["expected_monthly_savings_usd"]) <= Decimal(
            o["potential_monthly_savings_usd"]
        )


def test_exact_and_near_duplicates() -> None:
    def rec(user: str) -> TrafficRecord:
        return TrafficRecord(
            None, "claude-haiku-4-5", 20_000, 500, system_prompt="Answer FAQs.", user_content=user
        )

    recs = [
        rec("Reset password?"),
        rec("Reset password?"),
        rec("reset   PASSWORD"),
        rec("transfer 123 failed"),
        rec("transfer 456 failed"),
    ]
    r = build_report(recs, PRICES)
    kinds = {o["kind"]: o["affected_requests"] for o in r["opportunities"]}
    assert kinds.get("exact_cache") == 1 and kinds.get("semantic_cache") == 1
    # Different transfer ids are different questions, never a cache hit.
    assert normalize_light("transfer 123") != normalize_light("transfer 456")
    assert normalize("transfer 123") == normalize("transfer 456")


def test_csv_parsing_tolerance_and_unpriced_models() -> None:
    csv = (
        b"timestamp,model,input_tokens,output_tokens,cost_usd,workload\n"
        b"2026-09-01T00:00:00Z,claude-opus-5-5,1000,100,,triage\n"
        b"2026-09-02T00:00:00Z,mystery-model,1000,100,,triage\n"
        b"2026-09-02T00:00:00Z,claude-haiku-4-5,oops,100,,triage\n"
        b"2026-09-03T00:00:00Z,mystery-model,1000,100,0.02,triage\n"
    )
    parsed = parse(csv, "csv")
    assert len(parsed.records) == 3 and parsed.skipped == 1 and "row 4" in parsed.errors[0]
    r = build_report(parsed.records, PRICES)
    assert r["summary"]["unpriced_requests"] == 1
    assert [w["label"] for w in r["workloads"]] == ["triage"]  # explicit tag wins
    with pytest.raises(ValidationFailed, match="missing columns"):
        parse(b"model,foo\nx,1\n", "csv")
    with pytest.raises(ValidationFailed):
        parse(b"x", "xml")


def test_price_overrides_and_reported_costs() -> None:
    rec = TrafficRecord(None, "vendor-x", 1_000_000, 1_000_000)
    assert PriceBook(load_catalog()).cost(rec) is None
    assert PriceBook(load_catalog(), {"vendor-x": (Decimal(1), Decimal(2))}).cost(rec) == Decimal(3)
    reported = TrafficRecord(None, "claude-opus-5-5", 10, 10, cost_usd=Decimal("9"))
    assert PRICES.cost(reported) == Decimal("9")  # the source's own cost wins


def test_untemplated_traffic_groups_by_model_and_size() -> None:
    recs = [
        TrafficRecord(None, "m", 100, 1),
        TrafficRecord(None, "m", 120, 1),
        TrafficRecord(None, "m", 20_000, 1),
    ]
    keys = sorted(w.key for w in discover(recs))
    assert keys == ["untemplated:m:l", "untemplated:m:xs"]
