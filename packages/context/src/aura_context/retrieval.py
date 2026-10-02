"""Lexical relevance (BM25) and rank fusion. Pure."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

_WORD = re.compile(r"\w+", re.UNICODE)
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "as",
        "at",
        "by",
        "from",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "i",
        "you",
        "we",
        "they",
        "he",
        "she",
        "me",
        "my",
        "our",
        "your",
        "their",
        "please",
    ]
)


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def bm25_scores(
    query: str, docs: Sequence[str], *, k1: float = 1.5, b: float = 0.75
) -> list[float]:
    q = tokenize(query)
    tokenized = [tokenize(d) for d in docs]
    n = len(docs)
    if not n or not q:
        return [0.0] * n
    avgdl = sum(len(t) for t in tokenized) / n or 1.0
    df: Counter[str] = Counter()
    for t in tokenized:
        df.update(set(t))
    scores = []
    for t in tokenized:
        tf = Counter(t)
        dl = len(t)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], *, k: int = 60) -> dict[int, float]:
    """Fuse ranked lists of item indexes (best first). Robust to differently scaled
    scores, which is why hybrid retrieval uses it instead of adding raw scores."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank + 1)
    return fused
