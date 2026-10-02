"""Workload discovery. Pure.

Production LLM traffic is dominated by prompt *templates*: the same system prompt with
variables filled in. Normalizing variable-looking tokens (numbers, ids, emails, URLs,
dates) and fingerprinting the result groups requests into workloads deterministically
and explainably. An explicit workload tag in the logs always wins. Requests without a
system prompt fall back to the model plus a coarse input-size band.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from aura_xray.domain.records import TrafficRecord

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<uuid>",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b"), "<date>"),
    (re.compile(r"\b\d+(?:[.,]\d+)*\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "you",
        "your",
        "are",
        "is",
        "be",
        "as",
        "at",
        "by",
        "this",
        "that",
        "it",
        "from",
        "will",
        "can",
        "should",
        "must",
        "not",
        "if",
        "when",
        "then",
        "than",
        "into",
        "about",
        "all",
        "any",
        "each",
        "use",
        "using",
        "please",
        "assistant",
        "helpful",
        "answer",
        "respond",
        "response",
        "question",
        "user",
        "model",
        "n",
    ]
)


_PUNCT = re.compile(r"[^\w\s]")


def normalize_light(text: str) -> str:
    """Case, whitespace and punctuation only: keeps numbers and ids distinct."""
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", text.lower())).strip()


def normalize(text: str) -> str:
    out = text.lower()
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out.strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:12]


def _size_band(tokens: int) -> str:
    for limit, name in ((500, "xs"), (2_000, "s"), (8_000, "m"), (32_000, "l")):
        if tokens < limit:
            return name
    return "xl"


def workload_key(rec: TrafficRecord) -> str:
    if rec.workload_hint:
        return f"tag:{rec.workload_hint.strip().lower()[:60]}"
    if rec.system_prompt:
        return f"tpl:{fingerprint(rec.system_prompt)}"
    return f"untemplated:{rec.model}:{_size_band(rec.input_tokens)}"


@dataclass(frozen=True, slots=True)
class Workload:
    key: str
    label: str
    record_indexes: tuple[int, ...]


def _label(key: str, records: list[TrafficRecord]) -> str:
    if key.startswith("tag:"):
        return key[4:]
    if key.startswith("untemplated:"):
        _, model, band = key.split(":", 2)
        return f"Untemplated {model} ({band} inputs)"
    words: Counter[str] = Counter()
    for r in records[:50]:
        for w in re.findall(r"[a-z][a-z-]{2,}", normalize(r.system_prompt or "")):
            if w not in _STOP:
                words[w] += 1
    top = [w for w, _ in words.most_common(3)]
    return " · ".join(top) if top else key


TEMPLATE_SIMILARITY = 0.8
MAX_TEMPLATES_TO_CLUSTER = 5_000


def _tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z<>]+", normalize(text)))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def _merge_similar_templates(
    groups: dict[str, list[int]], records: list[TrafficRecord]
) -> dict[str, list[int]]:
    """Templates that differ only in unnormalizable variables (names, product titles)
    merge when their token sets have Jaccard similarity >= 0.8. Greedy, largest first."""
    tpl = sorted((k for k in groups if k.startswith("tpl:")), key=lambda k: -len(groups[k]))[
        :MAX_TEMPLATES_TO_CLUSTER
    ]
    reps: list[tuple[str, frozenset[str]]] = []
    merged: dict[str, list[int]] = {k: v for k, v in groups.items() if not k.startswith("tpl:")}
    for key in tpl:
        toks = _tokens(records[groups[key][0]].system_prompt or "")
        for rep_key, rep_toks in reps:
            if _jaccard(toks, rep_toks) >= TEMPLATE_SIMILARITY:
                merged[rep_key].extend(groups[key])
                break
        else:
            reps.append((key, toks))
            merged[key] = list(groups[key])
    clustered = set(tpl)
    for key in groups:  # templates beyond the clustering cap keep exact fingerprints
        if key.startswith("tpl:") and key not in clustered:
            merged[key] = list(groups[key])
    return merged


def discover(records: list[TrafficRecord]) -> list[Workload]:
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        groups.setdefault(workload_key(r), []).append(i)
    groups = _merge_similar_templates(groups, records)
    out = [
        Workload(key, _label(key, [records[i] for i in idx]), tuple(idx))
        for key, idx in groups.items()
    ]
    return sorted(out, key=lambda w: -len(w.record_indexes))
