"use client";

import { accounts, formatInr, plans } from "@aura/api-client";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage, PageTitle, useAsync } from "@/components/ui";

export default function Billing() {
  const { accountId } = useParams<{ accountId: string }>();
  const sub = useAsync(() => accounts.subscription(accountId), [accountId]);
  const catalog = useAsync(() => plans.list("b2b"), []);
  const credits = useAsync(() => accounts.credits(accountId), [accountId]);
  const [error, setError] = useState<string | null>(null);

  async function checkout(plan: string) {
    setError(null);
    try {
      const { checkout_url } = await accounts.checkout(accountId, plan);
      window.location.href = checkout_url;
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <>
      <PageTitle title="Billing" />
      <ErrorBox error={error ?? sub.error ?? catalog.error} />
      <div className="grid gap-4 md:grid-cols-3">
        {catalog.data?.map((p) => (
          <div key={p.code} className={`card ${sub.data?.plan_code === p.code ? "border-indigo-400" : ""}`}>
            <div className="font-semibold">{p.name}</div>
            <div className="mt-1 text-2xl font-bold">{formatInr(p.price_minor)}</div>
            {sub.data?.plan_code === p.code ? (
              <div className="mt-4 text-sm text-indigo-700">Current plan ({sub.data.status})</div>
            ) : p.price_minor ? (
              <button className="btn-primary mt-4" onClick={() => checkout(p.code)}>Switch to {p.name}</button>
            ) : (
              <a className="btn-secondary mt-4" href="mailto:sales@aura.ai">Contact sales</a>
            )}
          </div>
        ))}
      </div>
      <h2 className="mb-2 mt-10 text-lg font-semibold">Credits</h2>
      <p className="text-sm text-zinc-600">Balance: {credits.data?.balance.toLocaleString() ?? "-"}</p>
    </>
  );
}
