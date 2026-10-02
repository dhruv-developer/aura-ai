"use client";

import { admin } from "@aura/api-client";
import Link from "next/link";
import { useState } from "react";
import { ErrorBox, PageTitle, useAsync } from "@/components/ui";
import { date } from "@/lib/format";

export default function Accounts() {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const list = useAsync(() => admin.accounts({ q, kind, limit: 100 }), [q, kind]);
  return (
    <>
      <PageTitle title="Accounts" />
      <div className="mb-6 flex gap-3">
        <input className="input" placeholder="Search name, slug, email or account id" defaultValue={q}
          onKeyDown={(e) => { if (e.key === "Enter") setQ((e.target as HTMLInputElement).value); }} />
        <select className="input w-48" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All</option>
          <option value="individual">B2C users</option>
          <option value="organization">Organizations</option>
        </select>
      </div>
      <ErrorBox error={list.error} />
      <table className="table">
        <thead><tr><th>Account</th><th>Kind</th><th>Plan</th><th>Status</th><th>Created</th></tr></thead>
        <tbody>
          {list.data?.map((a) => (
            <tr key={a.account_id}>
              <td><Link className="text-indigo-600" href={`/accounts/${a.account_id}`}>{a.name}</Link></td>
              <td>{a.kind}</td>
              <td>{a.plan ?? "-"}</td>
              <td className={a.status === "suspended" ? "text-red-600" : ""}>{a.status}</td>
              <td>{date(a.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
