"use client";

import { admin } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, Loading, PageTitle, useAsync } from "@/components/ui";

type Detail = {
  account_id: string;
  kind: string;
  status: string;
  organization: { name: string; slug: string } | null;
  subscription: { plan: string; status: string; version: number } | null;
  usage: Record<string, { used: number; limit: number | null }>;
  credits_balance: number;
  members: { user_id: string; email: string; role: string }[];
  recent_audit: { action: string; actor: string | null; at: string }[];
};

export default function AccountDetail() {
  const { id } = useParams<{ id: string }>();
  const d = useAsync(() => admin.account(id) as Promise<Detail>, [id]);
  const [error, setError] = useState<string | null>(null);

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await d.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  const reason = (label: string) => window.prompt(`Reason for ${label} (audited):`)?.trim() ?? "";

  const a = d.data;
  return (
    <>
      <PageTitle title={a?.organization?.name ?? a?.account_id ?? "Account"}>
        {a?.status === "suspended" ? (
          <button className="btn-secondary" onClick={() => { const r = reason("restore"); if (r) void act(() => admin.restore(id, r)); }}>Restore</button>
        ) : (
          <button className="btn-danger" onClick={() => { const r = reason("suspension"); if (r) void act(() => admin.suspend(id, r)); }}>Suspend</button>
        )}
      </PageTitle>
      <ErrorBox error={error ?? d.error} />
      {d.loading && !a ? <Loading /> : null}
      {a ? (
        <div className="grid gap-6 md:grid-cols-2">
          <div className="card">
            <div className="font-semibold">Subscription</div>
            <p className="mt-2 text-sm">{a.subscription ? `${a.subscription.plan} (v${a.subscription.version}), ${a.subscription.status}` : "None"}</p>
            <form className="mt-4 flex gap-2" action={(f) => { const r = reason("plan change"); if (r) void act(() => admin.changePlan(id, String(f.get("plan")), r)); }}>
              <input className="input" name="plan" placeholder="plan code, e.g. growth" required />
              <button className="btn-secondary">Change</button>
            </form>
          </div>
          <div className="card">
            <div className="font-semibold">Credits</div>
            <p className="mt-2 text-sm">Balance: {a.credits_balance.toLocaleString()}</p>
            <form className="mt-4 flex gap-2" action={(f) => { const r = reason("credit grant"); if (r) void act(() => admin.credits(id, Number(f.get("amount")), r)); }}>
              <input className="input" name="amount" type="number" min={1} placeholder="amount" required />
              <button className="btn-secondary">Grant</button>
            </form>
          </div>
          <div className="card">
            <div className="font-semibold">Usage</div>
            <table className="table mt-2">
              <tbody>
                {Object.entries(a.usage).map(([k, v]) => (
                  <tr key={k}><td>{k}</td><td>{v.used.toLocaleString()} / {v.limit === null ? "∞" : v.limit.toLocaleString()}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card">
            <div className="font-semibold">Members</div>
            <ul className="mt-2 text-sm">
              {a.members.map((m) => <li key={m.user_id}>{m.email} ({m.role})</li>)}
            </ul>
          </div>
          <div className="card md:col-span-2">
            <div className="font-semibold">Recent activity</div>
            <table className="table mt-2">
              <tbody>
                {a.recent_audit.map((x, i) => (
                  <tr key={i}><td>{new Date(x.at).toLocaleString()}</td><td>{x.action}</td><td className="text-xs">{x.actor}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </>
  );
}
