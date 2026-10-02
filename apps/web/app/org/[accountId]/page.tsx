"use client";

import { accounts } from "@aura/api-client";
import { useParams } from "next/navigation";
import { ErrorBox, Loading, PageTitle, Stat, useAsync } from "@/components/ui";
import { date } from "@/lib/format";

export default function Overview() {
  const { accountId } = useParams<{ accountId: string }>();
  const sub = useAsync(() => accounts.subscription(accountId), [accountId]);
  const ents = useAsync(() => accounts.entitlements(accountId), [accountId]);
  const metered = (ents.data ?? []).filter((e) => e.enabled && e.used !== null && e.used !== undefined);
  return (
    <>
      <PageTitle title="Overview" />
      <ErrorBox error={sub.error ?? ents.error} />
      {sub.loading ? <Loading /> : null}
      {sub.data ? (
        <div className="grid gap-4 md:grid-cols-3">
          <Stat label="Plan" value={sub.data.plan_name} hint={`version ${sub.data.plan_version}`} />
          <Stat label="Status" value={sub.data.status} hint={sub.data.trial_ends_at ? `trial ends ${date(sub.data.trial_ends_at)}` : undefined} />
          <Stat label="Current period ends" value={date(sub.data.current_period_end)} />
        </div>
      ) : null}
      <h2 className="mb-3 mt-10 text-lg font-semibold">Usage this period</h2>
      <table className="table">
        <thead><tr><th>Metric</th><th>Used</th><th>Limit</th><th>Period</th></tr></thead>
        <tbody>
          {metered.map((e) => (
            <tr key={e.feature}>
              <td>{e.feature.replaceAll("_", " ")}</td>
              <td>{(e.used ?? 0).toLocaleString()}</td>
              <td>{e.limit === null ? "Unlimited" : e.limit.toLocaleString()}</td>
              <td>{e.period ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
