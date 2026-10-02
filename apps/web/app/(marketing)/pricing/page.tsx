import { formatInr, type Plan } from "@aura/api-client";
import Link from "next/link";

export const revalidate = 300;

async function getPlans(): Promise<Plan[]> {
  const base = process.env.AURA_API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${base}/v1/plans`, { next: { revalidate: 300 } });
    return res.ok ? ((await res.json()) as Plan[]) : [];
  } catch {
    return [];
  }
}

function PlanCard({ plan }: { plan: Plan }) {
  const highlights = plan.entitlements.filter((e) => e.enabled).slice(0, 8);
  return (
    <div className="card flex flex-col">
      <div className="text-lg font-semibold">{plan.name}</div>
      <div className="mt-2 text-3xl font-bold">
        {formatInr(plan.price_minor)}
        {plan.price_minor ? <span className="text-sm font-normal text-zinc-500">/{plan.interval}</span> : null}
      </div>
      {plan.trial_days ? <div className="text-sm text-indigo-600">{plan.trial_days}-day trial</div> : null}
      <ul className="mt-4 flex-1 space-y-1 text-sm text-zinc-600">
        {highlights.map((e) => (
          <li key={e.feature}>
            ✓ {e.feature.replaceAll("_", " ")}
            {e.limit !== null && e.period ? ` (${e.limit.toLocaleString()}/${e.period})` : ""}
          </li>
        ))}
      </ul>
      <Link href="/login" className="btn-primary mt-6">Get started</Link>
    </div>
  );
}

export default async function Pricing() {
  const plans = await getPlans();
  const b2c = plans.filter((p) => p.audience === "b2c");
  const b2b = plans.filter((p) => p.audience === "b2b");
  return (
    <section className="py-24">
      <h1 className="text-4xl font-bold tracking-tight">Pricing</h1>
      {plans.length === 0 ? <p className="mt-6 text-zinc-500">Pricing is temporarily unavailable.</p> : null}
      <h2 className="mt-12 text-xl font-semibold">AURA Personal</h2>
      <div className="mt-4 grid gap-6 md:grid-cols-3">{b2c.map((p) => <PlanCard key={p.code} plan={p} />)}</div>
      <h2 className="mt-16 text-xl font-semibold">AURA Business</h2>
      <div className="mt-4 grid gap-6 md:grid-cols-3">{b2b.map((p) => <PlanCard key={p.code} plan={p} />)}</div>
      <p className="mt-8 text-sm text-zinc-500">Enterprise plans are custom. <Link href="/enterprise">Contact sales.</Link></p>
    </section>
  );
}
