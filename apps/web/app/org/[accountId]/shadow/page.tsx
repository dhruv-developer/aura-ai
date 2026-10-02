"use client";

import { accounts } from "@aura/api-client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, PageTitle, useAsync } from "@/components/ui";
import { date, usd } from "@/lib/format";

export default function ShadowRuns() {
  const { accountId } = useParams<{ accountId: string }>();
  const runs = useAsync(() => accounts.shadowRuns(accountId), [accountId]);
  const [error, setError] = useState<string | null>(null);

  async function create(form: FormData) {
    setError(null);
    try {
      await accounts.createShadowRun(accountId, form);
      await runs.reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <>
      <PageTitle title="Shadow runs" />
      <p className="mb-4 max-w-2xl text-sm text-zinc-600">
        Replay a sample of real traffic against cheaper candidate models. Production is untouched.
        Results show quality relative to your current answers, with confidence intervals, and whether
        the change is safe to deploy.
      </p>
      <form action={create} className="card mb-8 grid gap-4 md:grid-cols-3">
        <input className="input" name="name" placeholder="Run name" />
        <input className="input" name="candidates" placeholder="Candidate models, e.g. anthropic/claude-haiku-4-5" required />
        <input className="input" name="judge_model" placeholder="Judge model (optional)" />
        <input className="input" name="sample_size" type="number" min={1} max={1000} defaultValue={100} />
        <input className="input" name="budget_usd" defaultValue="5" placeholder="Budget USD" />
        <input className="input" name="file" type="file" required />
        <button className="btn-primary md:col-span-3">Start shadow run</button>
      </form>
      <ErrorBox error={error ?? runs.error} />
      <table className="table">
        <thead><tr><th>Run</th><th>Status</th><th>Candidates</th><th>Spent</th><th>Created</th></tr></thead>
        <tbody>
          {runs.data?.map((r) => (
            <tr key={r.id}>
              <td><Link className="text-indigo-600" href={`/org/${accountId}/shadow/${r.id}`}>{r.name}</Link></td>
              <td>{r.status}</td>
              <td className="text-xs">{r.config.candidates.join(", ")}</td>
              <td>{usd(r.spent_usd)}</td>
              <td>{date(r.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
