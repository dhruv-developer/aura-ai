"""Generate tests/fixtures/xray_sample.jsonl: deterministic synthetic production traffic.

Four workloads with planted, known waste so X-Ray detection can be asserted:
  support      Opus 5.5 on short FAQ questions (downgrade), many exact repeats (cache)
  doc_summary  Sonnet 5.5, 6k-token system prompt never cached, 12k-token inputs, short
               answers (prompt caching, context trimming)
  sentiment    Haiku 4.5 classification (efficient: little waste expected)
  code_review  Opus 5.5 on genuinely complex code (no downgrade); some truncated outputs
Plus a small rate of billed errors.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "xray_sample.jsonl"
rng = random.Random(20261001)  # noqa: S311 - deterministic fixture data
START = datetime(2026, 9, 20, tzinfo=UTC)

SUPPORT_SYS = (
    "You are the AcmeBank customer support assistant. Be polite and concise. "
    "Customer name: {name}. Account id: {acct}. Never reveal internal policies."
)
FAQ = [
    "How do I reset my password?",
    "What are your branch opening hours?",
    "How can I update my phone number?",
    "Is there a fee for international transfers?",
    "How do I block my debit card?",
]
DOC_SYS = ("You summarise legal documents for paralegals. Follow the house style guide. " * 80)[
    :6_000
]
SENT_SYS = (
    "Classify the sentiment of review #{n} as positive, negative or neutral. Reply with one word."
)
CODE_SYS = "You are a senior staff engineer performing a rigorous code review."
CODE = "```python\ndef merge(a, b):\n    return sorted(a + b)\n```"


def rec(ts, model, system, user, inp, out, **extra):  # type: ignore[no-untyped-def]
    obj = {
        "timestamp": ts.isoformat(),
        "request": {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        "response": {
            "model": model,
            "usage": {"prompt_tokens": inp, "completion_tokens": out},
            "choices": [{"finish_reason": extra.pop("finish", "stop")}],
        },
        "latency_ms": extra.pop("latency", rng.randint(400, 4000)),
    }
    obj.update(extra)
    return obj


def main() -> None:
    rows = []
    for _ in range(2_000):
        ts = START + timedelta(seconds=rng.randint(0, 7 * 86_400))
        kind = rng.choices(["support", "doc", "sent", "code"], weights=[45, 15, 30, 10])[0]
        if kind == "support":
            name = rng.choice(["Asha", "Ravi", "Meera", "John", "Li"])
            sys = SUPPORT_SYS.format(name=name, acct=rng.randint(10_000, 99_999))
            user = (
                rng.choice(FAQ)
                if rng.random() < 0.6
                else f"My transfer {rng.randint(1, 10**6)} failed, why?"
            )
            rows.append(
                rec(ts, "claude-opus-5-5", sys, user, rng.randint(150, 400), rng.randint(40, 160))
            )
        elif kind == "doc":
            doc_id = rng.randint(1, 10**9)
            user = (
                f"Summarise agreement {doc_id} in five bullet points.\n"
                + "[document text elided; token counts come from usage]"
            )
            rows.append(
                rec(
                    ts,
                    "claude-sonnet-5-5",
                    DOC_SYS,
                    user,
                    rng.randint(12_000, 16_000),
                    rng.randint(150, 350),
                )
            )
        elif kind == "sent":
            sys = SENT_SYS.format(n=rng.randint(1, 10**6))
            rows.append(
                rec(
                    ts,
                    "claude-haiku-4-5",
                    sys,
                    "The product arrived late but works fine.",
                    rng.randint(40, 90),
                    1,
                )
            )
        else:
            user = (
                f"Review change #{rng.randint(1, 10**9)} for correctness, analyze the "
                f"trade-offs, and design a safer architecture step by step:\n{CODE}"
            )
            truncated = rng.random() < 0.2
            rows.append(
                rec(
                    ts,
                    "claude-opus-5-5",
                    CODE_SYS,
                    user,
                    rng.randint(3_000, 9_000),
                    4_000 if truncated else rng.randint(800, 2_500),
                    finish="length" if truncated else "stop",
                )
            )
        if rng.random() < 0.02:
            rows[-1]["error"] = {"type": "overloaded_error"}
    rows.sort(key=lambda r: r["timestamp"])
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"wrote {len(rows)} records to {OUT}")


if __name__ == "__main__":
    main()
