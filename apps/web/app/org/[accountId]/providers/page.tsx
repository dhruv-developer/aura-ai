"use client";

import { accounts } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, PageTitle, useAsync } from "@/components/ui";
import { date } from "@/lib/format";

export default function ProviderKeys() {
  const { accountId } = useParams<{ accountId: string }>();
  const creds = useAsync(() => accounts.providerCredentials(accountId), [accountId]);
  const [error, setError] = useState<string | null>(null);

  async function add(form: FormData) {
    setError(null);
    try {
      await accounts.addProviderCredential(accountId, {
        provider: String(form.get("provider")),
        api_key: String(form.get("api_key")),
        label: String(form.get("label") ?? ""),
      });
      await creds.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <>
      <PageTitle title="Provider keys (BYOK)" />
      <p className="mb-4 max-w-2xl text-sm text-zinc-600">
        Use your own provider accounts. Keys are encrypted at rest and only ever decrypted inside the
        runtime for a single call. Your provider bills you directly for these calls.
      </p>
      <form action={add} className="card mb-8 grid gap-4 md:grid-cols-4">
        <select className="input" name="provider" defaultValue="anthropic">
          <option value="anthropic">Anthropic</option>
          <option value="openai">OpenAI</option>
          <option value="openrouter">OpenRouter</option>
          <option value="groq">Groq</option>
        </select>
        <input className="input" name="label" placeholder="Label" />
        <input className="input" name="api_key" type="password" placeholder="API key" autoComplete="off" required />
        <button className="btn-primary">Add key</button>
      </form>
      <ErrorBox error={error ?? creds.error} />
      <table className="table">
        <thead><tr><th>Provider</th><th>Label</th><th>Key</th><th>Added</th><th></th></tr></thead>
        <tbody>
          {creds.data?.map((c) => (
            <tr key={c.id} className={c.revoked_at ? "text-zinc-400" : ""}>
              <td>{c.provider}</td>
              <td>{c.label}</td>
              <td>••••{c.last4}</td>
              <td>{date(c.created_at)}</td>
              <td>
                {c.revoked_at ? "Revoked" : (
                  <button className="btn-danger" onClick={() => accounts.revokeProviderCredential(accountId, c.id).then(creds.reload)}>
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
