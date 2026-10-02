import Link from "next/link";

const choices = [
  "a small model", "a frontier model", "web search", "your documents", "Python", "a database",
  "more context", "or no AI model at all",
];

export default function ForIndividuals() {
  return (
    <section className="py-24">
      <h1 className="max-w-3xl text-5xl font-bold tracking-tight">
        Let AURA figure out how your AI task should be solved.
      </h1>
      <p className="mt-6 max-w-2xl text-lg text-zinc-600">
        One AI experience. The right context, models and tools are chosen automatically for every task.
      </p>
      <div className="mt-8 flex gap-3">
        <Link href="/login?next=/app" className="btn-primary">Try AURA Free</Link>
        <Link href="/product" className="btn-secondary">See How It Works</Link>
      </div>
      <div className="mt-16 card max-w-2xl">
        <p className="font-medium">What if you didn&apos;t have to decide which AI model to use?</p>
        <p className="mt-2 text-zinc-600">Give AURA the task. It decides whether you need:</p>
        <ul className="mt-3 grid grid-cols-2 gap-1 text-zinc-700">
          {choices.map((c) => (
            <li key={c}>• {c}</li>
          ))}
        </ul>
      </div>
    </section>
  );
}
