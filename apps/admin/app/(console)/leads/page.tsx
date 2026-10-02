"use client";

import { admin } from "@aura/api-client";
import { ErrorBox, PageTitle, useAsync } from "@/components/ui";
import { date, usd } from "@/lib/format";

const STAGES = ["new", "contacted", "audit", "shadow", "pilot", "won", "lost"];

export default function Leads() {
  const leads = useAsync(() => admin.leads(), []);
  return (
    <>
      <PageTitle title="Leads & pilots" />
      <ErrorBox error={leads.error} />
      <table className="table">
        <thead><tr><th>Email</th><th>Company</th><th>Spend $/mo</th><th>Savings $/mo</th><th>Stage</th><th>Created</th></tr></thead>
        <tbody>
          {leads.data?.map((l) => (
            <tr key={l.id}>
              <td>{l.email}</td>
              <td>{l.company ?? "-"}</td>
              <td>{usd(String(l.summary.monthly_spend_usd ?? 0))}</td>
              <td>{usd(String(l.summary.est_monthly_savings_usd ?? 0))}</td>
              <td>
                <select className="input" defaultValue={l.stage} onChange={(e) => void admin.setLeadStage(l.id, e.target.value)}>
                  {STAGES.map((s) => <option key={s}>{s}</option>)}
                </select>
              </td>
              <td>{date(l.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
