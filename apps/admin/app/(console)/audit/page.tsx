"use client";

import { admin } from "@aura/api-client";
import { ErrorBox, PageTitle, useAsync } from "@/components/ui";
import { date } from "@/lib/format";

export default function Audit() {
  const log = useAsync(() => admin.audit(), []);
  return (
    <>
      <PageTitle title="Audit log" />
      <ErrorBox error={log.error} />
      <table className="table">
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Account</th></tr></thead>
        <tbody>
          {log.data?.map((a) => (
            <tr key={a.id}>
              <td>{date(a.at)}</td>
              <td className="text-xs">{a.actor_type}: {a.actor_id}</td>
              <td>{a.action}</td>
              <td className="text-xs">{a.account_id ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
