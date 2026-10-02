const stages = [
  ["Policy", "Your quality, cost, latency and compliance constraints, merged stricter-wins."],
  ["Route", "Every request is scored and sent to the cheapest model that meets the constraints."],
  ["Execute", "Automatic failover across models, circuit breaking, and streaming."],
  ["Measure", "Cost, latency and quality signals on every execution."],
  ["Optimize", "X-Ray finds waste; shadow runs prove alternatives before anything changes."],
];

export default function Product() {
  return (
    <section className="py-24">
      <h1 className="text-4xl font-bold tracking-tight">How AURA works</h1>
      <ol className="mt-10 space-y-4">
        {stages.map(([t, d], i) => (
          <li key={t} className="card flex gap-4">
            <span className="text-2xl font-bold text-indigo-600">{i + 1}</span>
            <div>
              <div className="font-semibold">{t}</div>
              <p className="text-zinc-600">{d}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
