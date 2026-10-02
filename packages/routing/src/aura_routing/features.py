"""Task features for routing. Pure and cheap (runs on every request)."""

from __future__ import annotations

import re

from aura_providers.domain.types import ChatRequest

Features = dict[str, float | int | str | bool]

_REASONING = re.compile(
    r"\b(prove|derive|analy[sz]e|compare|trade-?offs?|step[- ]by[- ]step|why|design|"
    r"architect\w*|optimi[sz]e|debug|refactor|evaluate|critique|plan|strategy|reason)\b",
    re.IGNORECASE,
)
_CODE = re.compile(r"```|\bdef \w+\(|\bclass \w+|\bfunction\s*\w*\(|=>|#include|;\s*$", re.M)
_MATH = re.compile(
    r"\\(frac|int|sum|sqrt)|\b(theorem|lemma|integral|derivative|equation|matrix|"
    r"probability)\b|\d\s*[\^*/]\s*\d",
    re.IGNORECASE,
)


def extract_features(req: ChatRequest, *, est_input_tokens: int, max_tokens: int) -> Features:
    text = req.text()
    last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return {
        "est_input_tokens": est_input_tokens,
        "requested_output_tokens": max_tokens,
        "n_messages": len(req.messages),
        "has_system": any(m.role == "system" for m in req.messages),
        "reasoning_cues": len(_REASONING.findall(last_user)),
        "has_code": bool(_CODE.search(text)),
        "has_math": bool(_MATH.search(text)),
        "non_ascii_ratio": round(non_ascii / len(text), 3) if text else 0.0,
        "last_user_chars": len(last_user),
    }


def complexity_score(f: Features) -> float:
    """0..1. Deliberately simple and explainable. V4 replaces this with a learned model
    trained on the features logged here."""
    score = 0.15
    score += min(int(f["reasoning_cues"]), 3) * 0.12
    score += 0.15 if f["has_code"] else 0.0
    score += 0.15 if f["has_math"] else 0.0
    tokens = int(f["est_input_tokens"])
    score += 0.10 if tokens > 2_000 else 0.0
    score += 0.10 if tokens > 16_000 else 0.0
    score += 0.05 if int(f["n_messages"]) > 6 else 0.0
    score += 0.10 if int(f["requested_output_tokens"]) > 4_000 else 0.0
    return round(min(score, 1.0), 3)
