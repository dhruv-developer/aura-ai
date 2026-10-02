"use client";

import { admin } from "@aura/api-client";
import Link from "next/link";
import { useState } from "react";
import { ErrorBox, Loading, PageTitle, Stat, useAsync } from "@/components/ui";

const inr = (v: string | undefined) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(Number(v ?? 0));

/** Spec §76/§130: consumer gross margin (revenue − platform inference − infra). */
export default function Economics() {
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const r = useAsync(() => admin.economics(month), [month]);
  const d = r.data;
  return (
    <>
      <PageTitle title="Consumer unit economics">
        <input type="month" className="input" value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month" />
      </PageTitle>
      <ErrorBox error={r.error} />
      {r.loading ? <Loading /> : null}
      {d ? (
        <>
          {d.alert ? (
            <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
              Consumer margin is negative this month. Review free-tier limits and plan pricing.
            </div>
          ) : null}
          <div className="grid gap-4 md:grid-cols-4">
            <Stat label="Revenue" value={inr(d.revenue_inr)} />
            <Stat label="Inference + infra" value={inr(d.cost_inr)} />
            <Stat label="Margin" value={inr(d.margin_inr)} hint={d.gross_margin_pct === null ? "no revenue" : `${d.gross_margin_pct.toFixed(1)}% gross`} />
            <Stat label="Loss-making accounts" value={`${d.negative_margin_accounts} / ${d.accounts}`} />
          </div>
          <h2 className="mb-3 mt-10 text-lg font-semibold">Worst accounts</h2>
          <table className="table">
            <thead><tr><th>Account</th><th>Revenue</th><th>Cost</th><th>Margin</th></tr></thead>
            <tbody>
              {d.worst.map((a) => (
                <tr key={a.account_id}>
                  <td><Link className="text-indigo-600" href={`/accounts/${a.account_id}`}>{a.account_id}</Link></td>
                  <td>{inr(a.revenue_inr)}</td>
                  <td>{inr(a.cost_inr)}</td>
                  <td className="text-red-700">{inr(a.margin_inr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </>
  );
}
