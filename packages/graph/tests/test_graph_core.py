import pytest

from aura_graph.intent import Risk, TaskType, compile_intent
from aura_graph.ir import Graph, Limits, Node, NodeKind, validate
from aura_graph.planner import plan
from aura_shared.errors import ValidationFailed


def _g(*nodes: Node, output: str = "a") -> Graph:
    return Graph(tuple(nodes), output)


def test_validate_levels_and_rejections() -> None:
    g = _g(
        Node("a", NodeKind.LLM, inputs=("b", "c")),
        Node("b", NodeKind.TOOL),
        Node("c", NodeKind.TOOL),
    )
    assert validate(g) == [["b", "c"], ["a"]]  # b and c run in parallel
    with pytest.raises(ValidationFailed, match="cycle"):
        validate(_g(Node("a", NodeKind.LLM, inputs=("b",)), Node("b", NodeKind.LLM, inputs=("a",))))
    with pytest.raises(ValidationFailed, match="unknown"):
        validate(_g(Node("a", NodeKind.LLM, inputs=("ghost",))))
    with pytest.raises(ValidationFailed, match="itself"):
        validate(_g(Node("a", NodeKind.LLM, inputs=("a",))))
    chain = tuple(
        Node(f"n{i}", NodeKind.LLM, inputs=(f"n{i - 1}",) if i else ()) for i in range(10)
    )
    with pytest.raises(ValidationFailed, match="depth"):
        validate(Graph(chain, "n9"), Limits(max_depth=5))
    with pytest.raises(ValidationFailed, match="nodes"):
        validate(Graph(chain, "n9"), Limits(max_nodes=3))
    assert Graph.from_dict(g.to_dict()) == g


def test_intent_compiler() -> None:
    assert compile_intent("what is 17 * (3 + 4)?").task is TaskType.MATH
    assert compile_intent("Write a python function to merge two lists").task is TaskType.CODE
    assert compile_intent("Summarize this report").task is TaskType.SUMMARIZE
    research = compile_intent("Research competitors in the Indian fintech market", can_search=True)
    assert research.task is TaskType.RESEARCH and "search" in research.needs
    assert compile_intent("hello there").task is TaskType.CHAT
    assert compile_intent("what's our refund policy?", has_knowledge=True).task is TaskType.QA
    assert compile_intent("Is this dosage safe?").risk is Risk.HIGH
    assert compile_intent("explain the refund policy").risk is Risk.MEDIUM


def test_planner_templates_validate() -> None:
    goals = [
        ("2+2", {}),
        ("Write python code to sort a list", {}),
        ("hello", {}),
        ("Research the market for EV chargers", {"can_search": True}),
        ("what is the refund policy", {"has_knowledge": True}),
        ("Analyze the legal contract risks", {"has_knowledge": True, "can_search": True}),
    ]
    for goal, kw in goals:
        g = plan(compile_intent(goal, **kw), goal=goal, kb_scopes=[("kb", "handbook")])
        validate(g)
    qa = plan(
        compile_intent("what is the refund policy", has_knowledge=True),
        goal="x",
        kb_scopes=[("kb", "h")],
    )
    assert [n.id for n in qa.nodes] == ["retrieve", "answer", "verify"] and qa.output == "verify"
    legal = plan(
        compile_intent("Analyze the legal contract risks", has_knowledge=True, can_search=True),
        goal="x",
        kb_scopes=[("kb", "h")],
    )
    assert legal.node("verify").params["level"] == "strong"  # high risk → strong verification
