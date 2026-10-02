"use client";

import { accounts } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useEffect } from "react";
import { ErrorBox, Loading, PageTitle, useAsync } from "@/components/ui";
import { pct, usd } from "@/lib/format";

export default function ShadowDetail() {
  const { accountId, runId } = useParams<{ accountId: string; runId: string }>();
  const run = useAsync(() => accounts.shadowRun(accountId, runId), [accountId, runId]);
  const pending = run.data?.status === "queued" || run.data?.status === "running";
  const { reload } = run;
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(() => void reload(), 5000);
    return () => clearInterval(t);
  }, [pending, reload]);

  const r = run.data;
  return (
    <>
      <PageTitle title={r?.name ?? "Shadow run"} />
      <ErrorBox error={run.error ?? r?.error ?? null} />
      {run.loading && !r ? <Loading /> : null}
      {r ? (
        <>
          <div className="mb-6 text-sm text-zinc-600">
            Status: <b>{r.status}</b> · Workload: {r.config.workload} · Samples: {r.config.sample_size} · Spent {usd(r.spent_usd)} of {usd(r.config.budget_usd)}
            {r.results.budget_exhausted ? " · stopped early: budget reached" : ""}
          </div>
          <table className="table">
            <thead>
              <tr><th>Candidate</th><th>Samples</th><th>Not worse than current</th><th>95% CI</th><th>Cost change</th><th>Savings $/mo</th><th>Decision</th></tr>
            </thead>
            <tbody>
              {(r.results.candidates ?? []).map((c) => (
                <tr key={c.model}>
                  <td>{c.model}</td>
                  <td>{c.samples}</td>
                  <td>{pct((c.non_inferiority_rate ?? 0) * 100)}</td>
                  <td>{c.non_inferiority_ci ? `${pct(c.non_inferiority_ci[0] * 100)}–${pct(c.non_inferiority_ci[1] * 100)}` : "-"}</td>
                  <td>{pct(c.cost_change_pct)}</td>
                  <td>{usd(c.projection?.monthly_savings_usd)}</td>
                  <td title={c.gate_reasons?.join("\n")}>{c.deploy_recommended ? "✅ Safe to deploy" : "⛔ Not yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {r.results.method_note ? <p className="mt-6 text-xs text-zinc-500">{r.results.method_note}</p> : null}
        </>
      ) : null}
    </>
  );
}
