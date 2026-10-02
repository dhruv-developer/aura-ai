import Link from "next/link";

export default function Home() {
  return (
    <section className="py-24">
      <p className="text-sm font-semibold uppercase tracking-wide text-indigo-600">AI Execution Optimization</p>
      <h1 className="mt-3 max-w-3xl text-5xl font-bold tracking-tight">
        You provide the goal. AURA decides how it should be computed.
      </h1>
      <p className="mt-6 max-w-2xl text-lg text-zinc-600">
        AURA is an AI execution engine that determines the context, prompts, models, tools and
        workflows needed to achieve a task at the quality, cost and latency you need.
      </p>
      <div className="mt-12 grid gap-6 md:grid-cols-2">
        <Link href="/for-individuals" className="card hover:border-indigo-300">
          <div className="text-sm font-medium text-indigo-600">AURA Personal</div>
          <div className="mt-2 text-xl font-semibold">Better AI results with less effort</div>
          <p className="mt-2 text-zinc-600">One AI experience that picks the right models and tools for every task.</p>
        </Link>
        <Link href="/for-startups" className="card hover:border-indigo-300">
          <div className="text-sm font-medium text-indigo-600">AURA Business</div>
          <div className="mt-2 text-xl font-semibold">Cut your AI bill without cutting quality</div>
          <p className="mt-2 text-zinc-600">Optimize production AI workloads against their real traffic, starting in shadow mode.</p>
        </Link>
      </div>
    </section>
  );
}
