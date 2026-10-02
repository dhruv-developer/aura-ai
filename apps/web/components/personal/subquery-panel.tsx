"use client";

import type { StoredAura } from "@aura/api-client";

import { usd } from "@/lib/format";

type SubqueryAura = Extract<StoredAura, { kind: "subqueries" }>;

/** Auto mode split a multi-part question: show each sub-question and the model that
 * answered it, which is where the savings come from. */
export function SubqueryPanel({ aura, questions = [] }: { aura: SubqueryAura; questions?: string[] }) {
  return (
    <details className="mt-2 rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
      <summary className="cursor-pointer select-none font-medium text-zinc-700 dark:text-zinc-200">
        How AURA optimized this
        <span className="ml-2 text-indigo-600 dark:text-indigo-300">
          split into {aura.subqueries.length} sub-questions
        </span>
      </summary>
      <ol className="mt-2 space-y-1.5">
        {aura.subqueries.map((p, i) => (
          <li key={i} className="flex items-start justify-between gap-4">
            <span>
              <span className="mr-1 text-zinc-400">{i + 1}.</span>
              {questions[i] ?? `Part ${i + 1}`}
            </span>
            <span className="shrink-0 font-mono text-[11px] text-zinc-500">
              {p.model ?? "failed"} · {usd(p.cost_usd)}
            </span>
          </li>
        ))}
      </ol>
      <div className="mt-2 flex justify-between border-t border-zinc-200 pt-2 dark:border-zinc-800">
        <span>Combined by {aura.combine_model ?? "n/a"}</span>
        <span>Total {usd(aura.cost_usd)}</span>
      </div>
      {aura.status !== "completed" ? (
        <div className="mt-1 text-amber-600 dark:text-amber-400">Some parts could not be answered fully ({aura.status}).</div>
      ) : null}
    </details>
  );
}
