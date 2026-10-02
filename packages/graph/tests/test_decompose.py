import pytest

from aura_graph.decompose import plan_subqueries, split_subqueries, worth_llm_split
from aura_graph.ir import Limits, NodeKind, validate


def test_numbered_and_bulleted_lists_split() -> None:
    text = (
        "Help me plan:\n1. What is the best time to visit Goa?\n"
        "2. How much does a hotel cost there?\n3) Which beaches are quiet?"
    )
    assert split_subqueries(text) == [
        "What is the best time to visit Goa?",
        "How much does a hotel cost there?",
        "Which beaches are quiet?",
    ]
    bullets = "- explain how vaccines train immunity\n* list three common side effects"
    assert len(split_subqueries(bullets)) == 2


def test_multiple_questions_split_and_single_questions_do_not() -> None:
    assert split_subqueries("What is RAG? How does it reduce hallucinations?") == [
        "What is RAG?",
        "How does it reduce hallucinations?",
    ]
    assert split_subqueries("What is the capital of France?") == []
    assert split_subqueries("Write a poem about the sea") == []
    assert split_subqueries("Why? How?") == []  # too short to be real sub-questions
    many = "".join(f"What is item number {i} about? " for i in range(9))
    assert len(split_subqueries(many)) == 5  # capped


def test_llm_split_only_for_long_compound_requests() -> None:
    assert not worth_llm_split("compare postgres and mysql")
    long = (
        "I am choosing a database for a fintech startup and want you to compare postgres "
        "and mysql on reliability, cost of hosting, ecosystem and hiring in India"
    )
    assert worth_llm_split(long)


def test_subquery_plan_is_parallel_then_combined() -> None:
    g = plan_subqueries("goal", ["What is A here?", "What is B here?", "What is C here?"])
    levels = validate(g, Limits())
    assert levels[0] == ["sq1", "sq2", "sq3"] and levels[1] == ["answer"]
    assert g.node("sq2").params["prompt"] == "What is B here?"
    assert g.node("answer").kind is NodeKind.SYNTHESIZE and g.output == "answer"
    with pytest.raises(ValueError):
        plan_subqueries("goal", ["only one"])


def test_split_subqueries_as_tasks() -> None:
    text = "1. What is RAG?\n2. How does it reduce hallucinations?"
    # Questions mode (default)
    assert split_subqueries(text, as_tasks=False) == [
        "What is RAG?",
        "How does it reduce hallucinations?",
    ]
    # Tasks mode (as_tasks=True)
    assert split_subqueries(text, as_tasks=True) == [
        "What is RAG",
        "How does it reduce hallucinations",
    ]


def test_plan_subqueries_as_tasks() -> None:
    sqs = ["Analyze RAG architecture", "Evaluate hallucination benchmarks"]
    g = plan_subqueries("goal", sqs, as_tasks=True)
    levels = validate(g, Limits())
    assert levels[0] == ["sq1", "sq2"] and levels[1] == ["answer"]
    assert g.node("sq1").params["prompt"] == "Analyze RAG architecture"
    assert "sub-task" in g.node("answer").params["instructions"]

