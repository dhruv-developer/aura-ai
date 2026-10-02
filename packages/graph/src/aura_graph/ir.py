"""Execution Graph IR (spec §48): the graph itself is an optimization object. Pure.

Nodes are typed. Edges are the ``inputs`` (ids of upstream nodes whose outputs this node
consumes). The validator rejects cycles, dangling references, oversized graphs and
excessive depth before anything executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from aura_shared.errors import ValidationFailed


class NodeKind(StrEnum):
    LLM = "llm"  # a model call; params: role, tier, instructions
    TOOL = "tool"  # params: tool, args
    RETRIEVAL = "retrieval"  # params: scopes, k
    VERIFY = "verify"  # params: level (light|strong)
    SYNTHESIZE = "synthesize"  # final answer assembly (an LLM call over inputs)
    HUMAN_APPROVAL = "human_approval"  # pauses the run until a person approves its input


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    kind: NodeKind
    params: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()
    max_retries: int = 1


@dataclass(frozen=True, slots=True)
class Graph:
    nodes: tuple[Node, ...]
    output: str
    template: str = "custom"

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "output": self.output,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind.value,
                    "params": n.params,
                    "inputs": list(n.inputs),
                    "max_retries": n.max_retries,
                }
                for n in self.nodes
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Graph:
        try:
            nodes = tuple(
                Node(
                    n["id"],
                    NodeKind(n["kind"]),
                    dict(n.get("params") or {}),
                    tuple(n.get("inputs") or ()),
                    int(n.get("max_retries", 1)),
                )
                for n in d["nodes"]
            )
            return cls(nodes, d["output"], d.get("template", "custom"))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValidationFailed(f"invalid graph: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Limits:
    max_nodes: int = 20
    max_depth: int = 8
    max_retries_total: int = 10
    max_events: int = 500
    max_wall_ms: int = 300_000


def validate(graph: Graph, limits: Limits | None = None) -> list[list[str]]:
    """Return the nodes grouped into topological *levels* (each level can run in
    parallel), or raise ValidationFailed."""
    lim = limits or Limits()
    ids = [n.id for n in graph.nodes]
    if not graph.nodes:
        raise ValidationFailed("graph has no nodes")
    if len(ids) != len(set(ids)):
        raise ValidationFailed("duplicate node ids")
    if len(ids) > lim.max_nodes:
        raise ValidationFailed(f"graph has {len(ids)} nodes; limit is {lim.max_nodes}")
    if graph.output not in ids:
        raise ValidationFailed("graph output node does not exist")
    known = set(ids)
    for n in graph.nodes:
        if missing := set(n.inputs) - known:
            raise ValidationFailed(f"node {n.id} depends on unknown nodes {sorted(missing)}")
        if n.id in n.inputs:
            raise ValidationFailed(f"node {n.id} depends on itself")
        if not 0 <= n.max_retries <= 3:
            raise ValidationFailed(f"node {n.id}: max_retries must be 0-3")
    if sum(n.max_retries for n in graph.nodes) > lim.max_retries_total:
        raise ValidationFailed("graph retry budget exceeds the limit")
    # Kahn's algorithm by levels; leftover nodes mean a cycle.
    remaining = {n.id: set(n.inputs) for n in graph.nodes}
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(i for i, deps in remaining.items() if not deps)
        if not ready:
            raise ValidationFailed(f"graph has a cycle among {sorted(remaining)}")
        levels.append(ready)
        for i in ready:
            del remaining[i]
        for deps in remaining.values():
            deps.difference_update(ready)
    if len(levels) > lim.max_depth:
        raise ValidationFailed(f"graph depth {len(levels)} exceeds limit {lim.max_depth}")
    return levels
