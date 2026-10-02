"""Fast token *estimates* for routing and budget pre-checks. Pure.

This is a heuristic (~4 characters per token plus per-message overhead) and is never
used for billing. Billing always uses the usage the provider reports.
"""

from __future__ import annotations

import math

from aura_providers.domain.types import ChatRequest

PER_MESSAGE_OVERHEAD = 4


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0


def estimate_input_tokens(req: ChatRequest) -> int:
    return sum(estimate_text_tokens(m.content) + PER_MESSAGE_OVERHEAD for m in req.messages)
