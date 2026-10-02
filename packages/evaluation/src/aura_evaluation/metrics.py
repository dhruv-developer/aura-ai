"""Deterministic quality signals. Pure."""

from __future__ import annotations

import json
import re
from collections import Counter

_WORD = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def token_f1(candidate: str, reference: str) -> float:
    """SQuAD-style token F1: robust overlap score for free-text answers (0..1)."""
    c, r = _tokens(candidate), _tokens(reference)
    if not c and not r:
        return 1.0
    if not c or not r:
        return 0.0
    common = sum((Counter(c) & Counter(r)).values())
    if common == 0:
        return 0.0
    precision, recall = common / len(c), common / len(r)
    return 2 * precision * recall / (precision + recall)


def is_json(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def rule_check(candidate: str, baseline: str) -> tuple[bool, str | None]:
    """Hard failures a candidate must never introduce relative to the baseline."""
    if not candidate.strip():
        return False, "empty output"
    if is_json(baseline) and not is_json(candidate):
        return False, "baseline was valid JSON, candidate is not"
    return True, None
