from aura_context import bm25_scores, compile_documents, compress_history, reciprocal_rank_fusion
from aura_context.compiler import chunk
from aura_providers.domain.tokens import estimate_text_tokens
from aura_providers.domain.types import Message

FILLER = [
    f"Section {i}. The quarterly facilities report covers parking allocation, cafeteria "
    f"menus, office plants and elevator maintenance schedules for building {i}."
    for i in range(60)
]
NEEDLE = (
    "Refund policy: customers may request a full refund within 30 days of purchase by "
    "emailing billing support with their order number."
)


def test_needle_survives_heavy_compression() -> None:
    docs = ["\n\n".join([*FILLER[:30], NEEDLE, *FILLER[30:]])]
    out = compile_documents(docs, "How many days do I have to request a refund?", token_budget=200)
    assert "30 days" in out.text
    assert out.tokens_after <= 200 and out.reduction > 0.85
    assert out.chunks_kept < out.chunks_total
    assert "context compiled" in out.summary()


def test_relevant_chunks_sit_at_the_edges() -> None:
    docs = ["\n\n".join([*FILLER[:10], NEEDLE])]
    out = compile_documents(docs, "refund within 30 days", token_budget=400)
    parts = out.text.split("\n\n")
    assert parts[0] == NEEDLE  # strongest first


def test_duplicates_removed_and_within_budget_unchanged() -> None:
    para = "Our API rate limit is 100 requests per minute per key."
    docs = ["\n\n".join([para, para, para + " ", *FILLER[:40]])]
    out = compile_documents(docs, "what is the API rate limit", token_budget=150)
    assert out.duplicates_removed >= 2 and out.text.count("rate limit is 100") == 1
    small = compile_documents(["short doc"], "q", token_budget=1000)
    assert small.text == "short doc" and small.tokens_after == small.tokens_before


def test_embedding_ranking_is_fused() -> None:
    docs = ["\n\n".join([FILLER[0], FILLER[1], NEEDLE])]
    # An embedding ranker that (wrongly) prefers chunk 1 cannot fully override BM25's
    # strong lexical match: RRF keeps the needle in a tight budget.
    out = compile_documents(docs, "refund 30 days", token_budget=80, embedding_ranking=[1, 0, 2])
    assert "refund" in out.text.lower()


def test_chunking_splits_long_paragraphs() -> None:
    long_para = " ".join(f"Sentence number {i} has some words." for i in range(400))
    parts = chunk(long_para)
    assert len(parts) > 1 and all(estimate_text_tokens(p) <= 320 for p in parts)


def test_history_compression_keeps_system_and_recent_turns() -> None:
    msgs = [Message("system", "You are helpful.")]
    for i in range(20):
        msgs.append(Message("user", f"Question {i}: " + "background detail " * 80))
        msgs.append(Message("assistant", f"Answer {i}: " + "long explanation " * 80))
    out, note = compress_history(msgs, token_budget=3000, keep_recent=4)
    assert note is not None and "history compressed" in note
    assert out[0].role == "system" and out[1].role == "user"
    assert out[-4:] == msgs[-4:]  # recent turns verbatim
    assert sum(estimate_text_tokens(m.content) for m in out) <= 3000
    same, none = compress_history(msgs[:3], token_budget=10_000)
    assert same == msgs[:3] and none is None


def test_bm25_and_rrf() -> None:
    s = bm25_scores("refund policy", ["refund policy details", "parking", "policy only"])
    assert s[0] > s[2] > s[1] == 0
    fused = reciprocal_rank_fusion([[0, 1, 2], [2, 0, 1]])
    assert max(fused, key=fused.__getitem__) == 0
