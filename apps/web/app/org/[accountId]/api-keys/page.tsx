"use client";

import { accounts } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, PageTitle, useAsync } from "@/components/ui";
import { date } from "@/lib/format";

export default function ApiKeys() {
  const { accountId } = useParams<{ accountId: string }>();
  const keys = useAsync(() => accounts.apiKeys(accountId), [accountId]);
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function create(form: FormData) {
    setError(null);
    try {
      const k = await accounts.createApiKey(accountId, { name: String(form.get("name") ?? "") });
      setCreated(k.key);
      await keys.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function revoke(id: string) {
    if (!confirm("Revoke this key? Applications using it stop working immediately.")) return;
    try {
      await accounts.revokeApiKey(accountId, id);
      await keys.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <>
      <PageTitle title="API keys" />
      <form action={create} className="card mb-6 flex gap-3">
        <input className="input" name="name" placeholder="Key name, e.g. production runtime" required />
        <button className="btn-primary whitespace-nowrap">Create key</button>
      </form>
      {created ? (
        <div className="card mb-6 border-amber-300 bg-amber-50">
          <div className="text-sm font-medium">Copy this key now. It will not be shown again.</div>
          <code className="mt-2 block break-all text-sm">{created}</code>
        </div>
      ) : null}
      <ErrorBox error={error ?? keys.error} />
      <table className="table">
        <thead><tr><th>Name</th><th>Prefix</th><th>Permissions</th><th>Last used</th><th></th></tr></thead>
        <tbody>
          {keys.data?.map((k) => (
            <tr key={k.id} className={k.revoked_at ? "text-zinc-400" : ""}>
              <td>{k.name}</td>
              <td><code>{k.prefix}</code></td>
              <td className="text-xs">{k.permissions.join(", ")}</td>
              <td>{date(k.last_used_at)}</td>
              <td>{k.revoked_at ? "Revoked" : <button className="btn-danger" onClick={() => revoke(k.id)}>Revoke</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
