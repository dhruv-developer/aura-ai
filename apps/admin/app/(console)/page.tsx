"use client";

import { admin } from "@aura/api-client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { ErrorBox, Loading, PageTitle, Stat, useAsync } from "@/components/ui";

const inr = (v: string | undefined) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(Number(v ?? 0));

export default function Overview() {
  const router = useRouter();
  const m = useAsync(() => admin.metrics(), []);
  const needsAuth = m.error !== null && /admin session|authentication/i.test(m.error);
  useEffect(() => {
    if (needsAuth) router.replace("/totp");
  }, [needsAuth, router]);
  const d = m.data;
  return (
    <>
      <PageTitle title="Platform overview" />
      <ErrorBox error={needsAuth ? null : m.error} />
      {m.loading ? <Loading /> : null}
      {d ? (
        <>
          <div className="grid gap-4 md:grid-cols-4">
            <Stat label="MRR" value={inr(d.mrr_inr.total)} hint={`B2C ${inr(d.mrr_inr.b2c)} · B2B ${inr(d.mrr_inr.b2b)}`} />
            <Stat label="ARR" value={inr(d.arr_inr)} />
            <Stat label="Users" value={String(d.users)} />
            <Stat label="Organizations" value={String(d.organizations)} hint={`${String(d.b2c_accounts)} personal accounts`} />
            <Stat label="Leads" value={String(d.leads)} />
            <Stat label="X-Ray reports" value={String(d.xray_reports)} />
            <Stat label="Shadow runs" value={String(d.shadow_runs)} />
            <Stat label="Suspended accounts" value={String(d.suspended_accounts)} />
          </div>
          <h2 className="mb-3 mt-10 text-lg font-semibold">Live subscriptions by plan</h2>
          <table className="table">
            <tbody>
              {Object.entries((d.live_subscriptions_by_plan ?? {}) as Record<string, number>).map(([plan, n]) => (
                <tr key={plan}><td>{plan}</td><td>{n}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </>
  );
}
