"""Template rendering and deterministic candidate rewrites. Pure.

Candidates are *proposals*. None reaches production without being evaluated against
the current prompt on the customer's own inputs (optimizer.py) and passing the gate.
"""

from __future__ import annotations

import re

from aura_shared.errors import ValidationFailed

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")

FILLER = [
    (re.compile(r"\bplease note that\s+", re.I), ""),
    (re.compile(r"\bit is (very |really )?important (that you |to )", re.I), ""),
    (re.compile(r"\bmake sure (that )?you (always )?", re.I), ""),
    (re.compile(r"\bI want you to\s+", re.I), ""),
    (re.compile(r"\byou should always\s+", re.I), "always "),
    (re.compile(r"\b(very|really|extremely|basically|actually)\s+", re.I), ""),
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bdue to the fact that\b", re.I), "because"),
]
LENGTH_HINTS = re.compile(r"\b(concise|brief|short|words?|sentences?|bullet|length)\b", re.I)


def variables(template: str) -> set[str]:
    return set(_VAR.findall(template))


def render(template: str, values: dict[str, str]) -> str:
    missing = variables(template) - set(values)
    if missing:
        raise ValidationFailed(f"missing prompt variables: {', '.join(sorted(missing))}")
    return _VAR.sub(lambda m: str(values[m.group(1)]), template)


def _normalize_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return out.strip()


def _dedupe_instructions(text: str) -> str:
    seen: set[str] = set()
    kept = []
    for part in _SENT.split(text):
        key = re.sub(r"\W+", " ", part.lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(part.strip())
    return " ".join(p for p in kept if p)


def _strip_filler(text: str) -> str:
    for pattern, repl in FILLER:
        text = pattern.sub(repl, text)
    # Re-capitalize sentence starts that lost their first word.
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _concise(text: str) -> str:
    if LENGTH_HINTS.search(text):
        return text
    return text.rstrip() + "\nRespond concisely: no preamble, no repetition."


def candidates(template: str) -> list[tuple[str, str]]:
    """(name, candidate) pairs, de-duplicated and excluding no-op rewrites. Variables
    are preserved by every transform (they are opaque tokens to the rewrites)."""
    base = _normalize_whitespace(template)
    proposals = [
        ("whitespace", base),
        ("dedupe", _dedupe_instructions(base)),
        ("filler", _strip_filler(base)),
        ("concise", _concise(base)),
        ("combined", _concise(_strip_filler(_dedupe_instructions(base)))),
    ]
    out, seen = [], {template.strip()}
    for name, text in proposals:
        if text and text not in seen and variables(text) == variables(template):
            seen.add(text)
            out.append((name, text))
    return out
