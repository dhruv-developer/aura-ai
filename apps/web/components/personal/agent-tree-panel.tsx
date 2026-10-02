"use client";

import type { AgentNode, AgentSummary, MessageTrace } from "@aura/api-client";

import { shortModel } from "@/components/personal/agent-timeline";
import { Markdown } from "@/components/personal/markdown";
import { usd } from "@/lib/format";

const BADGE: Record<AgentNode["status"], string> = {
  approved: "bg-emerald-100 text-emerald-800",
  unresolved: "bg-amber-100 text-amber-800",
  unreviewed: "bg-amber-100 text-amber-800",
  skipped_cap: "bg-zinc-100 text-zinc-600",
  failed: "bg-red-100 text-red-800",
  pending: "bg-zinc-100 text-zinc-600",
};

function NodeView({ node }: { node: AgentNode }) {
  const cost = node.turns.reduce((s, t) => s + Number(t.cost_usd), 0);
  return (
    <li className="mt-2">
      <details>
        <summary className="cursor-pointer select-none">
          <span className="text-zinc-400">{node.id} </span>
          {node.question}
          <span className={`ml-2 rounded px-1.5 py-0.5 text-[10px] ${BADGE[node.status]}`}>{node.status.replace("_", " ")}</span>
          <span className="ml-2 text-[11px] text-zinc-400">
            {node.turns.filter((t) => t.role === "drafter").length} draft(s) · {usd(cost)}
            {node.origin === "follow_up" ? " · follow-up" : ""}
          </span>
        </summary>
        {/* The drafter ↔ reviewer conversation for this sub-question. */}
        <ol className="mt-1 space-y-1 border-l border-zinc-200 pl-3 dark:border-zinc-800">
          {node.turns.map((t, i) => (
            <li key={i}>
              {t.role === "drafter" ? (
                <details>
                  <summary className="cursor-pointer select-none">
                    ✎ Drafter <span className="font-mono text-[10px] text-zinc-400">{shortModel(t.model)} · {usd(t.cost_usd)}</span>
                  </summary>
                  <div className="mt-1 rounded bg-white p-2 text-[12px] dark:bg-zinc-950">
                    <Markdown text={t.text} />
                  </div>
                </details>
              ) : (
                <div>
                  🔍 Reviewer <span className="font-mono text-[10px] text-zinc-400">{shortModel(t.model)} · {usd(t.cost_usd)}</span>
                  {t.parsed ? (
                    <span className={t.verdict === "approve" ? "ml-2 text-emerald-600" : "ml-2 text-amber-600"}>
                      {t.verdict === "approve" ? "approved" : "asked for changes"}
                      {t.score ? ` (score ${t.score}/5)` : ""}
                    </span>
                  ) : (
                    <span className="ml-2 text-zinc-400">unreadable review (skipped)</span>
                  )}
                  {t.issues.length ? (
                    <ul className="ml-5 list-disc text-[11px] text-zinc-600">
                      {t.issues.map((issue, j) => (
                        <li key={j}>{issue}</li>
                      ))}
                    </ul>
                  ) : null}
                  {t.follow_ups.length ? (
                    <div className="ml-5 text-[11px] text-indigo-600">Follow-ups: {t.follow_ups.join(" · ")}</div>
                  ) : null}
                </div>
              )}
            </li>
          ))}
        </ol>
        {node.children.length ? (
          <ul className="ml-4">
            {node.children.map((c) => (
              <NodeView key={c.id} node={c} />
            ))}
          </ul>
        ) : null}
      </details>
    </li>
  );
}

/** "How AURA optimized this" for multi-agent answers: totals plus the reviewed tree. */
export function AgentTreePanel({ aura, trace }: { aura: AgentSummary; trace: MessageTrace | null | undefined }) {
  const tree = trace && "tree" in trace ? trace.tree : [];
  return (
    <details className="mt-2 rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
      <summary className="cursor-pointer select-none font-medium text-zinc-700 dark:text-zinc-200">
        How AURA optimized this
        <span className="ml-2 font-normal text-indigo-600 dark:text-indigo-300">
          {aura.nodes} part{aura.nodes === 1 ? "" : "s"} · {aura.reviews} review{aura.reviews === 1 ? "" : "s"}
          {aura.revisions ? ` · ${aura.revisions} revision${aura.revisions === 1 ? "" : "s"}` : ""}
          {aura.follow_ups ? ` · ${aura.follow_ups} follow-up${aura.follow_ups === 1 ? "" : "s"}` : ""}
        </span>
      </summary>
      <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1">
        <dt>Drafter</dt>
        <dd className="font-mono">{aura.models.drafter.map(shortModel).join(", ") || "n/a"}</dd>
        <dt>Reviewer</dt>
        <dd className="font-mono">{aura.models.reviewer.map(shortModel).join(", ") || "n/a"}</dd>
        <dt>Finaliser</dt>
        <dd className="font-mono">{aura.models.finaliser ? shortModel(aura.models.finaliser) : "not needed (one reviewed answer)"}</dd>
        <dt>Cost / calls / time</dt>
        <dd>
          {usd(aura.cost_usd)} · {aura.calls} model calls · {(aura.ms / 1000).toFixed(1)} s
        </dd>
      </dl>
      {aura.warnings.length ? <div className="mt-1 text-amber-600">{aura.warnings.join(" · ")}</div> : null}
      {tree.length ? <ul className="mt-2">{tree.map((n) => <NodeView key={n.id} node={n} />)}</ul> : null}
    </details>
  );
}
