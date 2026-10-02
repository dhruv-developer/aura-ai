"""Personal-data detection and redaction (spec §21, §70). Pure.

Detectors favour precision: checksummed identifiers (cards: Luhn, Aadhaar: Verhoeff) are
validated, so ordinary numbers are not mangled. This is a policy control, not a DLP
product: it catches the common, high-risk identifiers before any provider sees them.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from aura_policy.policy import PiiMode
from aura_shared.errors import AuraError

_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


@dataclass(frozen=True, slots=True)
class _Detector:
    kind: str
    pattern: re.Pattern[str]
    valid: Callable[[str], bool] = lambda _: True


_DETECTORS = (
    _Detector("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    _Detector(
        "SECRET",
        re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|aura_sk_[A-Za-z0-9_-]{20,})"),
    ),
    _Detector(
        "CARD",
        re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
        lambda m: 13 <= len(_digits(m)) <= 19 and luhn_ok(_digits(m)),
    ),
    _Detector(
        "AADHAAR",
        re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"),
        lambda m: verhoeff_ok(_digits(m)),
    ),
    _Detector("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    _Detector("PHONE", re.compile(r"(?:\+91[ -]?|\b0)?\b[6-9]\d{4}[ -]?\d{5}\b")),
)


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    start: int
    end: int


def detect(text: str) -> list[Finding]:
    """Non-overlapping findings, earliest detector wins on overlap."""
    taken: list[Finding] = []
    for det in _DETECTORS:
        for m in det.pattern.finditer(text):
            if not det.valid(m.group()):
                continue
            if any(m.start() < f.end and f.start < m.end() for f in taken):
                continue
            taken.append(Finding(det.kind, m.start(), m.end()))
    return sorted(taken, key=lambda f: f.start)


def redact(text: str) -> tuple[str, list[str]]:
    """Replace findings with ``[KIND]`` placeholders. Returns (text, kinds found)."""
    findings = detect(text)
    if not findings:
        return text, []
    out, last = [], 0
    for f in findings:
        out.append(text[last : f.start])
        out.append(f"[{f.kind}]")
        last = f.end
    out.append(text[last:])
    return "".join(out), [f.kind for f in findings]


class PiiBlocked(AuraError):
    status, code, title = 422, "pii_blocked", "Request contains personal data"


class _HasMessages(Protocol):
    @property
    def messages(self) -> tuple[Any, ...]: ...


def apply_pii_policy[R: _HasMessages](req: R, mode: PiiMode) -> tuple[R, list[str]]:
    """Redact or block personal data in every message of a request (any frozen
    dataclass with ``messages`` whose items have ``content``). Runs after memory and
    context injection, so nothing a provider sees escapes it. Notes carry only the kinds
    found, never the values."""
    if mode is PiiMode.ALLOW:
        return req, []
    kinds: list[str] = []
    messages = []
    for m in req.messages:
        text, found = redact(m.content)
        kinds.extend(found)
        messages.append(dataclasses.replace(m, content=text) if found else m)
    if not kinds:
        return req, []
    summary = ", ".join(sorted({k.lower() for k in kinds}))
    if mode is PiiMode.BLOCK:
        raise PiiBlocked(f"this policy blocks personal data ({summary})")
    return dataclasses.replace(req, messages=tuple(messages)), [  # type: ignore[type-var]
        f"pii: redacted {len(kinds)} items ({summary})"
    ]
