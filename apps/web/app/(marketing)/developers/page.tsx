const curl = `curl https://api.aura.ai/v1/chat/completions \\
  -H "Authorization: Bearer $AURA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "aura/auto",
       "messages": [{"role": "user", "content": "Summarise this ticket"}],
       "aura": {"max_cost_usd": 0.02, "max_latency_ms": 3000}}'`;

const python = `from openai import OpenAI

client = OpenAI(base_url="https://api.aura.ai/v1", api_key=AURA_API_KEY)
resp = client.chat.completions.create(
    model="aura/auto",
    messages=[{"role": "user", "content": "Summarise this ticket"}],
    extra_body={"aura": {"quality": "balanced"}},
)
print(resp.choices[0].message.content)`;

export default function Developers() {
  return (
    <section className="py-24">
      <h1 className="text-4xl font-bold tracking-tight">Developers</h1>
      <p className="mt-4 max-w-2xl text-zinc-600">
        AURA is OpenAI-compatible. Point your client at AURA, use <code>aura/auto</code> (or
        <code> aura/fast</code>, <code>aura/balanced</code>, <code>aura/best</code>), and add an
        optional <code>aura</code> block for budgets, latency, allowed models and residency.
        Every response includes an <code>aura</code> block that shows the routing decision and cost.
      </p>
      <h2 className="mt-10 text-lg font-semibold">cURL</h2>
      <pre className="mt-2 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-sm text-zinc-100">{curl}</pre>
      <h2 className="mt-8 text-lg font-semibold">Python (OpenAI SDK)</h2>
      <pre className="mt-2 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-sm text-zinc-100">{python}</pre>
    </section>
  );
}
