"use client";

import { accounts } from "@aura/api-client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, PageTitle, useAsync } from "@/components/ui";
import { date, pct, usd } from "@/lib/format";

export default function XRays() {
  const { accountId } = useParams<{ accountId: string }>();
  const list = useAsync(() => accounts.xrays(accountId), [accountId]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function upload(form: FormData) {
    setBusy(true);
    setError(null);
    try {
      await accounts.uploadXray(accountId, form);
      await list.reload();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageTitle title="AI Workload X-Ray" />
      <form action={upload} className="card mb-8 grid gap-4 md:grid-cols-4">
        <input className="input" name="name" placeholder="Report name" />
        <select className="input" name="format" defaultValue="jsonl">
          <option value="jsonl">JSONL</option>
          <option value="csv">CSV</option>
        </select>
        <input className="input" name="file" type="file" required />
        <button className="btn-primary" disabled={busy}>{busy ? "Analysing…" : "Run X-Ray"}</button>
        <input className="input md:col-span-4" name="prices" placeholder="Optional prices for unknown models: model=input/output USD per MTok, …" />
      </form>
      <ErrorBox error={error ?? list.error} />
      <table className="table">
        <thead><tr><th>Report</th><th>Requests</th><th>Spend $/mo</th><th>Expected savings</th><th>Created</th></tr></thead>
        <tbody>
          {list.data?.map((r) => (
            <tr key={r.id}>
              <td><Link className="text-indigo-600" href={`/org/${accountId}/xray/${r.id}`}>{r.name}</Link></td>
              <td>{r.record_count.toLocaleString()}</td>
              <td>{usd(r.summary.monthly_spend_usd)}</td>
              <td>{usd(r.summary.est_monthly_savings_usd)} ({pct(r.summary.est_savings_pct)})</td>
              <td>{date(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
