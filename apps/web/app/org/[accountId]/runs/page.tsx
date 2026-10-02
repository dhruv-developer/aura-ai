"use client";

import { runs, type GraphRunSummary } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, PageTitle, useAsync } from "@/components/ui";
import { date, usd } from "@/lib/format";

const KIND_COLOR: Record<string, string> = {
  llm: "border-indigo-300 bg-indigo-50",
  synthesize: "border-indigo-400 bg-indigo-100",
  tool: "border-amber-300 bg-amber-50",
  retrieval: "border-emerald-300 bg-emerald-50",
  verify: "border-purple-300 bg-purple-50",
  human_approval: "border-red-300 bg-red-50",
};

function GraphView({ run }: { run: GraphRunSummary }) {
  const nodes = run.trace.nodes ?? [];
  return (
    <div className="mt-4 flex flex-wrap items-start gap-3">
      {nodes.map((n, i) => (
        <div key={n.id} className="flex items-center gap-3">
          <div className={`min-w-40 rounded-lg border p-3 text-xs ${KIND_COLOR[n.kind] ?? "border-zinc-200"}`}>
            <div className="font-semibold">{n.id}</div>
            <div className="text-zinc-500">{n.kind} · {n.status}</div>
            {n.model ? <div className="mt-1 truncate">{n.model}</div> : null}
            <div className="mt-1">{usd(n.cost_usd)} · {n.latency_ms} ms</div>
            {n.error ? <div className="mt-1 text-red-600">{n.error}</div> : null}
          </div>
          {i < nodes.length - 1 ? <span className="text-zinc-300">→</span> : null}
        </div>
      ))}
    </div>
  );
}

export default function Runs() {
  const { accountId } = useParams<{ accountId: string }>();
  const list = useAsync(() => runs.list(accountId), [accountId]);
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <PageTitle title="Execution graph runs" />
      <ErrorBox error={list.error} />
      <table className="table">
        <thead><tr><th>Run</th><th>Template</th><th>Status</th><th>Escalations</th><th>Cost</th><th>Created</th></tr></thead>
        <tbody>
          {list.data?.map((r) => (
            <tr key={r.id} className="cursor-pointer" onClick={() => setOpen(open === r.id ? null : r.id)}>
              <td className="font-mono text-xs">{r.id}</td>
              <td>{r.template ?? "-"}</td>
              <td>{r.status}</td>
              <td>{r.trace.escalations ?? 0}</td>
              <td>{usd(r.cost_usd)}</td>
              <td>{date(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {list.data?.filter((r) => r.id === open).map((r) => (
        <div key={r.id} className="card mt-6">
          <div className="text-sm font-medium">Graph for {r.id}</div>
          <GraphView run={r} />
          {r.trace.warnings?.length ? <p className="mt-3 text-xs text-amber-700">{r.trace.warnings.join("; ")}</p> : null}
        </div>
      ))}
    </>
  );
}
