"use client";

import type { AuraBlock } from "@aura/api-client";

import { usd } from "@/lib/format";

const SOURCE: Record<AuraBlock["credential_source"], string> = {
  platform: "AURA credits",
  byok: "your own provider key (no credits used)",
  cache: "a cached answer (free)",
};

/** "How AURA optimized this" (spec §15): every decision behind one answer. */
export function OptimizedPanel({ aura }: { aura: AuraBlock }) {
  const baseline = aura.baseline_cost_usd ? Number(aura.baseline_cost_usd) : null;
  const saved = Number(aura.saved_usd);
  const savedPct = baseline && baseline > 0 ? (saved / baseline) * 100 : 0;
  return (
    <details className="mt-2 rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600">
      <summary className="cursor-pointer select-none font-medium text-zinc-700">
        How AURA optimized this
        {savedPct > 0 ? <span className="ml-2 text-emerald-700">saved {savedPct.toFixed(0)}%</span> : null}
      </summary>
      <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1">
        <dt>Model</dt>
        <dd className="font-mono">{aura.model} ({aura.tier})</dd>
        <dt>Why</dt>
        <dd>{aura.routing.reason}</dd>
        <dt>Cost</dt>
        <dd>
          {usd(aura.cost_usd)}
          {baseline !== null ? <> vs {usd(baseline)} on a frontier model</> : null}
        </dd>
        <dt>Paid with</dt>
        <dd>{SOURCE[aura.credential_source]}</dd>
        <dt>Tokens</dt>
        <dd>{aura.usage.input_tokens} in / {aura.usage.output_tokens} out</dd>
        <dt>Latency</dt>
        <dd>{(aura.latency_ms / 1000).toFixed(2)} s</dd>
        {aura.attempts.some((a) => a.error) ? (
          <>
            <dt>Fallbacks</dt>
            <dd>{aura.attempts.filter((a) => a.error).map((a) => `${a.model}: ${a.error}`).join("; ")}</dd>
          </>
        ) : null}
      </dl>
      {aura.notes.length > 0 ? (
        <ul className="mt-2 list-disc pl-5">
          {aura.notes.map((n) => <li key={n}>{n}</li>)}
        </ul>
      ) : null}
    </details>
  );
}
