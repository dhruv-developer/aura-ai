import Link from "next/link";

const steps = [
  ["AI Workload X-Ray", "Upload a sample of logs. See spend by workload, waste, and projected savings."],
  ["Shadow analysis", "AURA replays real traffic against cheaper policies. Production is untouched."],
  ["Pilot & canary", "Deploy proven policies gradually, with automatic rollback."],
  ["Autopilot", "Policies keep improving as your traffic changes."],
];

export default function ForStartups() {
  return (
    <section className="py-24">
      <h1 className="max-w-3xl text-5xl font-bold tracking-tight">Your AI bill is a workload problem.</h1>
      <p className="mt-6 max-w-2xl text-lg text-zinc-600">
        AURA automatically finds cheaper and faster execution strategies across models, context,
        prompts, retrieval, tools and workflows, without lowering your required quality.
      </p>
      <div className="mt-8 flex gap-3">
        <Link href="/xray" className="btn-primary">Get My AI Savings Report</Link>
        <a href="mailto:sales@aura.ai?subject=Technical%20demo" className="btn-secondary">Book a Technical Demo</a>
      </div>
      <div className="mt-16 grid gap-4 md:grid-cols-4">
        {steps.map(([title, body], i) => (
          <div key={title} className="card">
            <div className="text-xs text-zinc-400">Step {i + 1}</div>
            <div className="mt-1 font-semibold">{title}</div>
            <p className="mt-2 text-sm text-zinc-600">{body}</p>
          </div>
        ))}
      </div>
      <p className="mt-12 max-w-2xl text-zinc-600">
        Most AI workloads cost more than they need to because the execution path is never
        optimized as a whole. We start in shadow mode and show current versus optimized cost,
        quality and latency on your own traffic.
      </p>
    </section>
  );
}
