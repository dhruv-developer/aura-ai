"use client";

import { personal } from "@aura/api-client";
import { useState, type FormEvent } from "react";

import { ErrorBox, PageTitle, Stat, errorMessage, useAsync } from "@/components/ui";

export default function Invite() {
  const ref = useAsync(() => personal.referral(), []);
  const [code, setCode] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const link = ref.data && typeof window !== "undefined" ? `${window.location.origin}/login?ref=${ref.data.code}` : "";

  async function redeem(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await personal.redeem(code.trim());
      setResult(`${r.credits.toLocaleString()} credits added.`);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="max-w-2xl space-y-8">
      <PageTitle title="Invite friends" />
      <ErrorBox error={ref.error ?? error} />
      <p className="text-sm text-zinc-600">You both get 2,000 credits when a friend signs up with your code.</p>
      <div className="grid grid-cols-3 gap-4">
        <Stat label="Your code" value={ref.data?.code ?? "…"} />
        <Stat label="Friends joined" value={ref.data?.referred ?? 0} />
        <Stat label="Credits earned" value={(ref.data?.credits_earned ?? 0).toLocaleString()} />
      </div>
      {link ? (
        <div className="flex gap-2">
          <input className="input" readOnly value={link} aria-label="Invite link" />
          <button className="btn-secondary" onClick={() => navigator.clipboard?.writeText(link)}>Copy</button>
        </div>
      ) : null}
      <form onSubmit={redeem} className="card space-y-2">
        <label className="label" htmlFor="code">Got a code from a friend?</label>
        <div className="flex gap-2">
          <input id="code" className="input" value={code} onChange={(e) => setCode(e.target.value)} placeholder="ABCD1234" />
          <button className="btn-primary" disabled={!code.trim()}>Redeem</button>
        </div>
        {result ? <div className="text-sm text-emerald-700">{result}</div> : null}
      </form>
    </div>
  );
}
