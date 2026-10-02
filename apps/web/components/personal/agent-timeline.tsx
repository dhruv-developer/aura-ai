"use client";

import type { AgentEvent } from "@aura/api-client";

interface LiveNode {
  id: string;
  question: string;
  depth: number;
  state: string;
  attempt: number;
  drafter?: string;
  reviewer?: string;
  issues: string[];
}

export const shortModel = (m?: string | null) => (m ? (m.split("/").pop() ?? m) : "");

/** Fold the agents' event stream into the current state of the tree. */
export function liveTree(events: AgentEvent[]): { nodes: LiveNode[]; phase: string } {
  const nodes = new Map<string, LiveNode>();
  let phase = "Starting…";
  for (const e of events) {
    switch (e.type) {
      case "splitting":
        phase = "Splitting your question…";
        break;
      case "split":
        phase = `Working on ${e.nodes.length} part${e.nodes.length > 1 ? "s" : ""}…`;
        for (const n of e.nodes) nodes.set(n.id, { id: n.id, question: n.question, depth: 1, state: "drafting", attempt: 1, issues: [] });
        break;
      case "follow_up":
        nodes.set(e.node, { id: e.node, question: e.question, depth: e.depth, state: "drafting", attempt: 1, issues: [] });
        break;
      case "draft": {
        const n = nodes.get(e.node);
        if (n) Object.assign(n, { state: "reviewing", attempt: e.attempt, drafter: e.model });
        break;
      }
      case "review": {
        const n = nodes.get(e.node);
        if (n) Object.assign(n, { reviewer: e.model, issues: e.issues, state: e.verdict === "approve" ? "approved" : "revising" });
        break;
      }
      case "review_failed": {
        const n = nodes.get(e.node);
        if (n) Object.assign(n, { state: "unreviewed", issues: [e.reason] });
        break;
      }
      case "node_done": {
        const n = nodes.get(e.node);
        if (n) n.state = e.status;
        break;
      }
      case "finalise":
        phase = e.skipped ? "Done." : "Finalising the answer…";
        break;
      case "finalised":
        phase = `Finalised by ${shortModel(e.model)}.`;
        break;
    }
  }
  // Tree order: 1, 1.1, 1.1.1, 2 …
  const ordered = [...nodes.values()].sort((a, b) => {
    const pa = a.id.split(".").map(Number);
    const pb = b.id.split(".").map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
      const d = (pa[i] ?? -1) - (pb[i] ?? -1);
      if (d) return d;
    }
    return 0;
  });
  return { nodes: ordered, phase };
}

const STATE: Record<string, { icon: string; label: string; cls: string }> = {
  drafting: { icon: "✎", label: "drafting", cls: "text-zinc-500" },
  reviewing: { icon: "🔍", label: "in review", cls: "text-indigo-600" },
  revising: { icon: "↻", label: "revising", cls: "text-amber-600" },
  approved: { icon: "✓", label: "approved", cls: "text-emerald-600" },
  unresolved: { icon: "!", label: "unresolved", cls: "text-amber-600" },
  unreviewed: { icon: "?", label: "not reviewed", cls: "text-amber-600" },
  skipped_cap: { icon: "–", label: "skipped (limit)", cls: "text-zinc-400" },
  failed: { icon: "✕", label: "failed", cls: "text-red-600" },
};

/** Live view of the Drafter ↔ Reviewer work, as an indented tree. */
export function AgentTimeline({ events }: { events: AgentEvent[] }) {
  const { nodes, phase } = liveTree(events);
  return (
    <div className="mb-3 rounded-md border border-indigo-200 bg-indigo-50 p-3 text-xs dark:border-indigo-900">
      <div className="mb-1 font-medium text-indigo-700">{phase}</div>
      <ul className="space-y-1">
        {nodes.map((n) => {
          const s = STATE[n.state] ?? STATE.drafting!;
          return (
            <li key={n.id} style={{ paddingLeft: `${(n.depth - 1) * 16}px` }}>
              <div className="flex items-start gap-2">
                <span className={`w-4 shrink-0 text-center ${s.cls} ${n.state === "drafting" || n.state === "reviewing" ? "animate-pulse" : ""}`}>{s.icon}</span>
                <span className="min-w-0 flex-1">
                  <span className="text-zinc-400">{n.id} </span>
                  {n.question}
                  <span className={`ml-2 ${s.cls}`}>
                    {s.label}
                    {n.attempt > 1 ? ` · draft ${n.attempt}` : ""}
                  </span>
                  {n.drafter || n.reviewer ? (
                    <span className="ml-2 font-mono text-[10px] text-zinc-400">
                      {n.drafter ? `drafter ${shortModel(n.drafter)}` : ""}
                      {n.reviewer ? ` · reviewer ${shortModel(n.reviewer)}` : ""}
                    </span>
                  ) : null}
                  {(n.state === "revising" || n.state === "unreviewed") && n.issues.length ? (
                    <span className="block text-[11px] text-amber-700 dark:text-amber-400">Reviewer: {n.issues.join("; ")}</span>
                  ) : null}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
