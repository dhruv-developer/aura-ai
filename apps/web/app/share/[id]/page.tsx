"use client";

import { personal } from "@aura/api-client";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ErrorBox, Loading, useAsync } from "@/components/ui";
import { usd } from "@/lib/format";

/** Public "Optimized by AURA" card (spec §15). Holds aggregates only: no prompt, answer
 * or account details ever reach this page. */
export default function SharedCard() {
  const { id } = useParams<{ id: string }>();
  const { data, error, loading } = useAsync(() => personal.publicCard(id), [id]);
  return (
    <main className="mx-auto mt-20 max-w-md px-6">
      {loading ? <Loading /> : null}
      <ErrorBox error={error} />
      {data ? (
        <div className="card text-center">
          <div className="text-sm uppercase tracking-wide text-indigo-600">Optimized by AURA</div>
          <div className="mt-4 text-5xl font-bold text-emerald-600">{data.saved_pct.toFixed(0)}%</div>
          <div className="text-sm text-zinc-500">cheaper than a frontier model</div>
          <dl className="mt-6 grid grid-cols-2 gap-y-2 text-left text-sm">
            <dt className="text-zinc-500">Model</dt>
            <dd className="font-mono">{data.model}</dd>
            <dt className="text-zinc-500">Mode</dt>
            <dd>{data.mode}</dd>
            <dt className="text-zinc-500">Cost</dt>
            <dd>
              {usd(data.cost_usd)}
              {data.baseline_cost_usd ? ` vs ${usd(data.baseline_cost_usd)}` : ""}
            </dd>
            <dt className="text-zinc-500">Latency</dt>
            <dd>{(data.latency_ms / 1000).toFixed(2)} s</dd>
            {data.optimizations.length ? (
              <>
                <dt className="text-zinc-500">Tricks used</dt>
                <dd>{data.optimizations.join(", ")}</dd>
              </>
            ) : null}
          </dl>
          <Link href="/for-individuals" className="btn-primary mt-6">Try AURA free</Link>
        </div>
      ) : null}
    </main>
  );
}
