"use client";

import { accounts, formatInr, personal } from "@aura/api-client";
import { useState, type FormEvent } from "react";

import { usePersonal } from "@/components/personal/context";
import { ErrorBox, Loading, PageTitle, Stat, errorMessage, useAsync } from "@/components/ui";

declare global {
  interface Window {
    Razorpay?: new (options: Record<string, unknown>) => { open: () => void };
  }
}

function loadCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("Could not load the payment window"));
    document.body.appendChild(s);
  });
}

export default function Usage() {
  const { account, user } = usePersonal();
  const id = account.account_id;
  const credits = useAsync(() => accounts.credits(id), [id]);
  const ents = useAsync(() => accounts.entitlements(id), [id]);
  const packs = useAsync(() => personal.creditPacks(), []);
  const keys = useAsync(() => accounts.providerCredentials(id), [id]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");

  async function buy(pack: string) {
    setError(null);
    try {
      const order = await personal.buyCredits(id, pack);
      await loadCheckout();
      if (!window.Razorpay) throw new Error("Payments are unavailable right now");
      new window.Razorpay({
        key: order.key_id,
        order_id: order.order_id,
        amount: order.amount_minor,
        currency: order.currency,
        name: "AURA",
        description: "Credit pack",
        prefill: { email: user.email },
        // Credits arrive via the signed payment webhook, not this callback.
        handler: () => {
          setNotice("Payment received. Your credits will appear in a few seconds.");
          setTimeout(() => void credits.reload(), 4000);
        },
      }).open();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function addKey(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await accounts.addProviderCredential(id, { provider, api_key: apiKey, label: "personal" });
      setApiKey("");
      await keys.reload();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  // monthly_credits is an allowance granted into the balance, not a usage counter.
  const metered = (ents.data ?? []).filter(
    (e) => e.enabled && e.limit !== null && e.period && e.feature !== "monthly_credits",
  );
  const byok = (ents.data ?? []).some((e) => e.feature === "byok" && e.enabled);

  return (
    <div className="max-w-3xl space-y-8">
      <PageTitle title="Usage & credits" />
      <ErrorBox error={error ?? credits.error ?? ents.error} />
      {notice ? <div className="rounded-md bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</div> : null}

      <div className="grid grid-cols-3 gap-4">
        <Stat label="Credit balance" value={credits.data ? credits.data.balance.toLocaleString() : "…"} hint="1 credit ≈ $0.0001 of AI usage" />
        {metered.slice(0, 2).map((e) => (
          <Stat key={e.feature} label={e.feature.replaceAll("_", " ")} value={`${e.used ?? 0} / ${e.limit}`} hint={`per ${e.period}`} />
        ))}
      </div>

      <section>
        <h2 className="mb-3 font-semibold">Top up</h2>
        <div className="grid grid-cols-3 gap-4">
          {packs.data?.map((p) => (
            <div key={p.code} className="card text-center">
              <div className="text-lg font-semibold">{p.credits.toLocaleString()} credits</div>
              <div className="mt-1 text-sm text-zinc-500">{formatInr(p.price_minor)}</div>
              <button className="btn-primary mt-3 w-full" onClick={() => buy(p.code)}>Buy</button>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-1 font-semibold">Use your own provider key</h2>
        <p className="mb-3 text-sm text-zinc-600">
          Calls made with your own key cost no credits, and keep working when your credits run out.
        </p>
        {byok ? (
          <>
            <form onSubmit={addKey} className="flex gap-2">
              <select className="input w-40" value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="Provider">
                <option value="anthropic">Anthropic</option>
                <option value="openai">OpenAI</option>
                <option value="openrouter">OpenRouter</option>
                <option value="groq">Groq</option>
              </select>
              <input className="input" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="API key" aria-label="API key" />
              <button className="btn-primary" disabled={!apiKey}>Save</button>
            </form>
            <ul className="mt-3 text-sm text-zinc-600">
              {keys.data?.filter((k) => !k.revoked_at).map((k) => (
                <li key={k.id}>{k.provider}: …{k.last4}</li>
              ))}
            </ul>
          </>
        ) : (
          <p className="text-sm text-zinc-500">Available on Plus and Pro.</p>
        )}
      </section>

      <section>
        <h2 className="mb-3 font-semibold">History</h2>
        {credits.loading ? <Loading /> : null}
        <table className="table">
          <thead>
            <tr><th>When</th><th>What</th><th className="text-right">Credits</th></tr>
          </thead>
          <tbody>
            {credits.data?.history.map((h, i) => (
              <tr key={i}>
                <td>{new Date(h.created_at).toLocaleString()}</td>
                <td>{h.memo || h.kind}</td>
                <td className={`text-right ${h.amount < 0 ? "text-zinc-700" : "text-emerald-700"}`}>{h.amount > 0 ? `+${h.amount}` : h.amount}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
