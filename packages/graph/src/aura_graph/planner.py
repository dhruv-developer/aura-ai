"""Planner: intent → graph from templates (spec §48 example). Pure.

Verification depth follows risk (spec §57): low risk gets none, medium gets a light
(rule-based) check, and high risk gets a strong (model-judged) check.
"""

from __future__ import annotations

from aura_graph.intent import Intent, Risk, TaskType
from aura_graph.ir import Graph, Node, NodeKind


def _verify_level(risk: Risk) -> str | None:
    return {Risk.LOW: None, Risk.MEDIUM: "light", Risk.HIGH: "strong"}[risk]


def plan(intent: Intent, *, goal: str, kb_scopes: list[tuple[str, str]] | None = None) -> Graph:
    kb = [list(s) for s in (kb_scopes or [])]
    verify = _verify_level(intent.risk)
    t = intent.task

    if t is TaskType.MATH:
        return Graph(
            (Node("calc", NodeKind.TOOL, {"tool": "calculator", "args": {"expression": goal}}),),
            "calc",
            "math",
        )

    if t is TaskType.CODE:
        nodes = [
            Node(
                "write",
                NodeKind.LLM,
                {
                    "role": "coder",
                    "tier": "frontier",
                    "instructions": "Write the code. Return only a fenced python block.",
                },
            ),
            Node("run", NodeKind.TOOL, {"tool": "python", "args_from": "write"}, ("write",), 0),
            Node(
                "answer",
                NodeKind.SYNTHESIZE,
                {"tier": "medium", "instructions": "Explain the code and the execution result."},
                ("write", "run"),
            ),
        ]
        return Graph(tuple(nodes), "answer", "code")

    gather: list[Node] = []
    if "retrieval" in intent.needs and kb:
        gather.append(Node("retrieve", NodeKind.RETRIEVAL, {"scopes": kb, "k": 8}))
    if "search" in intent.needs:
        gather.append(
            Node("search", NodeKind.TOOL, {"tool": "web_search", "args": {"query": goal}})
        )
    gather_ids = tuple(n.id for n in gather)

    if t is TaskType.RESEARCH:
        nodes = [
            *gather,
            Node(
                "analyze",
                NodeKind.LLM,
                {
                    "role": "analyst",
                    "tier": "frontier",
                    "instructions": "Analyze the sources and the question.",
                },
                gather_ids,
            ),
        ]
        last = "analyze"
        if verify:
            nodes.append(
                Node(
                    "verify",
                    NodeKind.VERIFY,
                    {"level": verify, "target": "analyze"},
                    ("analyze", *gather_ids),
                )
            )
            last = "verify"
        nodes.append(
            Node(
                "answer",
                NodeKind.SYNTHESIZE,
                {"tier": "medium", "instructions": "Write the final answer."},
                ("analyze", last) if last != "analyze" else ("analyze",),
            )
        )
        return Graph(tuple(nodes), "answer", "research")

    tier = "small" if t in (TaskType.SUMMARIZE, TaskType.QA) else "auto"
    nodes = [
        *gather,
        Node(
            "answer",
            NodeKind.LLM,
            {"role": "assistant", "tier": tier, "instructions": ""},
            gather_ids,
        ),
    ]
    if verify:
        nodes.append(
            Node(
                "verify",
                NodeKind.VERIFY,
                {"level": verify, "target": "answer"},
                ("answer", *gather_ids),
            )
        )
        return Graph(tuple(nodes), "verify", t.value)
    return Graph(tuple(nodes), "answer", t.value)


APPROVAL_NODE = "approval"


def with_approval(graph: Graph) -> Graph:
    """Gate the final output behind a human decision (spec §57 "human" verification):
    nothing is released until a reviewer approves exactly the content they saw."""
    gate = Node(APPROVAL_NODE, NodeKind.HUMAN_APPROVAL, {"target": graph.output}, (graph.output,))
    return Graph((*graph.nodes, gate), APPROVAL_NODE, graph.template)
