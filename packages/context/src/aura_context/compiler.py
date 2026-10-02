"""Context compilation (spec §49). Pure and deterministic.

    documents → chunks → relevance (BM25, optionally fused with embedding similarity)
    → dedupe → extractive compression → greedy packing into the token budget
    → edge ordering (strongest chunks first and last)

Large models attend best to the beginning and end of long contexts ("lost in the
middle"), so the most relevant chunks are placed at the edges.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from aura_context.retrieval import bm25_scores, reciprocal_rank_fusion, tokenize
from aura_providers.domain.tokens import estimate_text_tokens
from aura_providers.domain.types import Message

_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?])\s+")
CHUNK_TOKENS = 300
NEAR_DUP_JACCARD = 0.85


@dataclass(frozen=True, slots=True)
class CompiledContext:
    text: str
    tokens_before: int
    tokens_after: int
    chunks_total: int
    chunks_kept: int
    duplicates_removed: int
    compressed_chunks: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reduction(self) -> float:
        return 1 - self.tokens_after / self.tokens_before if self.tokens_before else 0.0

    def summary(self) -> str:
        return (
            f"context compiled: {self.tokens_before:,} → {self.tokens_after:,} tokens "
            f"({self.chunks_kept}/{self.chunks_total} chunks, {self.duplicates_removed} "
            f"duplicates removed, {self.compressed_chunks} compressed)"
        )


def chunk(text: str, *, max_tokens: int = CHUNK_TOKENS) -> list[str]:
    """Paragraph-first chunking; oversized paragraphs split on sentence boundaries."""
    out: list[str] = []
    for para in (p.strip() for p in _PARA.split(text)):
        if not para:
            continue
        if estimate_text_tokens(para) <= max_tokens:
            out.append(para)
            continue
        buf: list[str] = []
        for sent in _SENT.split(para):
            if buf and estimate_text_tokens(" ".join([*buf, sent])) > max_tokens:
                out.append(" ".join(buf))
                buf = []
            buf.append(sent)
        if buf:
            out.append(" ".join(buf))
    return out


def _shingles(text: str) -> frozenset[str]:
    words = tokenize(text)
    return frozenset(" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2)))


def _dedupe(chunks: list[str]) -> tuple[list[int], int]:
    kept: list[int] = []
    seen_exact: set[str] = set()
    shingles: list[frozenset[str]] = []
    removed = 0
    for i, c in enumerate(chunks):
        h = hashlib.sha256(" ".join(tokenize(c)).encode()).hexdigest()
        sh = _shingles(c)
        near = any(
            len(sh & other) / len(sh | other) >= NEAR_DUP_JACCARD
            for other in shingles
            if sh | other
        )
        if h in seen_exact or near:
            removed += 1
            continue
        seen_exact.add(h)
        shingles.append(sh)
        kept.append(i)
    return kept, removed


def _compress(chunk_text: str, query: str, budget_tokens: int) -> str:
    """Keep the most query-relevant sentences (original order) within budget."""
    sents = [s for s in _SENT.split(chunk_text) if s.strip()]
    if len(sents) <= 1:
        return chunk_text
    scores = bm25_scores(query, sents)
    order = sorted(range(len(sents)), key=lambda i: (-scores[i], i))
    chosen: set[int] = set()
    used = 0
    for i in order:
        t = estimate_text_tokens(sents[i])
        if used + t > budget_tokens and chosen:
            continue
        chosen.add(i)
        used += t
    return " ".join(sents[i] for i in sorted(chosen))


def compile_documents(
    documents: Sequence[str],
    query: str,
    *,
    token_budget: int,
    embedding_ranking: Sequence[int] | None = None,
) -> CompiledContext:
    chunks = [c for d in documents for c in chunk(d)]
    before = sum(estimate_text_tokens(d) for d in documents)
    if not chunks:
        return CompiledContext("", before, 0, 0, 0, 0, 0)
    if before <= token_budget:
        joined = "\n\n".join(documents)
        return CompiledContext(
            joined,
            before,
            before,
            len(chunks),
            len(chunks),
            0,
            0,
            ("context within budget: unchanged",),
        )
    kept_idx, dups = _dedupe(chunks)
    lexical = bm25_scores(query, [chunks[i] for i in kept_idx])
    lex_rank = [kept_idx[i] for i in sorted(range(len(kept_idx)), key=lambda j: -lexical[j])]
    if embedding_ranking:
        allowed = set(kept_idx)
        fused = reciprocal_rank_fusion([lex_rank, [i for i in embedding_ranking if i in allowed]])
        ranked = sorted(fused, key=lambda i: -fused[i])
    else:
        ranked = lex_rank

    selected: list[tuple[int, str]] = []
    used = compressed = 0
    for i in ranked:
        text = chunks[i]
        t = estimate_text_tokens(text)
        remaining = token_budget - used
        if remaining <= 20:
            break
        if t > remaining:
            text = _compress(text, query, remaining)
            t = estimate_text_tokens(text)
            if t > remaining:
                continue
            compressed += 1
        selected.append((i, text))
        used += t

    # Edge ordering: rank 0 first, rank 1 last, rank 2 second, rank 3 second-to-last…
    front: list[str] = []
    back: list[str] = []
    for pos, (_, text) in enumerate(selected):
        (front if pos % 2 == 0 else back).append(text)
    ordered = front + back[::-1]
    text = "\n\n".join(ordered)
    return CompiledContext(
        text, before, estimate_text_tokens(text), len(chunks), len(selected), dups, compressed
    )


def compress_history(
    messages: Sequence[Message], *, token_budget: int, keep_recent: int = 6
) -> tuple[list[Message], str | None]:
    """Fit a long conversation into ``token_budget``. System messages and the most recent
    ``keep_recent`` turns stay verbatim. Older turns are compressed extractively against
    the latest user message, and dropped oldest-first if still over budget."""
    total = sum(estimate_text_tokens(m.content) for m in messages)
    if total <= token_budget:
        return list(messages), None
    system = [m for m in messages if m.role == "system"]
    convo = [m for m in messages if m.role != "system"]
    recent, older = convo[-keep_recent:], convo[:-keep_recent]
    if not older:
        return list(messages), None
    query = next((m.content for m in reversed(convo) if m.role == "user"), "")
    fixed = sum(estimate_text_tokens(m.content) for m in system + recent)
    room = max(token_budget - fixed, 0)
    per_turn = max(room // max(len(older), 1), 30)
    squeezed = [Message(m.role, _compress(m.content, query, per_turn)) for m in older]
    while squeezed and sum(estimate_text_tokens(m.content) for m in squeezed) > room:
        squeezed.pop(0)
    # Provider APIs need the conversation to start with a user turn.
    while squeezed and squeezed[0].role != "user":
        squeezed.pop(0)
    out = system + squeezed + recent
    after = sum(estimate_text_tokens(m.content) for m in out)
    return out, (
        f"history compressed: {total:,} → {after:,} tokens "
        f"({len(older) - len(squeezed)} oldest turns dropped)"
    )
