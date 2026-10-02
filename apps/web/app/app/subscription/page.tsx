"use client";

import { accounts, formatInr, orgs, plans } from "@aura/api-client";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { usePersonal } from "@/components/personal/context";
import { ErrorBox, Loading, PageTitle, errorMessage, useAsync } from "@/components/ui";

const HIGHLIGHTS: Record<string, string[]> = {
  free: ["300 tasks / month", "Smart routing", "1,000 credits / month"],
  plus: ["5,000 tasks / month", "Personal memory", "Bring your own keys", "20,000 credits / month"],
  pro: ["30,000 tasks / month", "Research mode", "API access", "100,000 credits / month"],
};

export default function Subscription() {
  const { account } = usePersonal();
  const router = useRouter();
  const id = account.account_id;
  const current = useAsync(() => accounts.subscription(id), [id]);
  const catalog = useAsync(() => plans.list("b2c"), []);
  const [error, setError] = useState<string | null>(null);
  const [team, setTeam] = useState("");

  async function upgrade(plan: string) {
    setError(null);
    try {
      const { checkout_url } = await accounts.checkout(id, plan);
      window.location.href = checkout_url;
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  // Spec §162: one person can use AURA personally and in a team; the org is separate.
  async function createTeam(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const org = await orgs.create(team.trim());
      router.push(`/org/${org.account_id}`);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="max-w-4xl space-y-10">
      <PageTitle title="Subscription" />
      <ErrorBox error={error ?? current.error ?? catalog.error} />
      {catalog.loading ? <Loading /> : null}
      <div className="grid grid-cols-3 gap-4">
        {catalog.data?.map((p) => {
          const active = current.data?.plan_code === p.code;
          return (
            <div key={p.code} className={`card ${active ? "border-indigo-500 ring-1 ring-indigo-500" : ""}`}>
              <div className="text-lg font-semibold">{p.name}</div>
              <div className="mt-1 text-2xl">
                {formatInr(p.price_minor)}
                {p.price_minor ? <span className="text-sm text-zinc-500">/{p.interval}</span> : null}
              </div>
              <ul className="mt-3 space-y-1 text-sm text-zinc-600">
                {(HIGHLIGHTS[p.code] ?? []).map((h) => <li key={h}>✓ {h}</li>)}
              </ul>
              {active ? (
                <div className="mt-4 text-sm font-medium text-indigo-700">Current plan</div>
              ) : p.price_minor ? (
                <button className="btn-primary mt-4 w-full" onClick={() => upgrade(p.code)}>Upgrade</button>
              ) : null}
            </div>
          );
        })}
      </div>

      <section className="card">
        <h2 className="font-semibold">Bring AURA to your team</h2>
        <p className="mt-1 text-sm text-zinc-600">
          Create an organization for shared workloads, API keys, X-Ray reports and optimized policies. Your personal
          account, memory and credits stay yours.
        </p>
        <form onSubmit={createTeam} className="mt-3 flex gap-2">
          <input className="input" value={team} onChange={(e) => setTeam(e.target.value)} placeholder="Company name" aria-label="Company name" />
          <button className="btn-primary" disabled={!team.trim()}>Create team</button>
        </form>
      </section>
    </div>
  );
}
