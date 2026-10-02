const controls = [
  ["Tenant isolation", "Every tenant table is protected by Postgres row-level security in addition to application scoping."],
  ["Encryption", "Provider keys and replay datasets are envelope-encrypted (AES-256-GCM) and bound to the owning tenant."],
  ["Data minimization", "X-Ray stores aggregates only; shadow replay inputs are erased when a run completes."],
  ["Credentials", "API keys are stored as keyed hashes and shown once. Sessions are server-side and revocable instantly."],
  ["Audit", "Sensitive actions are written to an append-only audit log."],
  ["Admin access", "Staff access requires TOTP and short-lived admin sessions."],
];

export default function Security() {
  return (
    <section className="py-24">
      <h1 className="text-4xl font-bold tracking-tight">Security</h1>
      <div className="mt-10 grid gap-4 md:grid-cols-2">
        {controls.map(([t, d]) => (
          <div key={t} className="card">
            <div className="font-semibold">{t}</div>
            <p className="mt-1 text-sm text-zinc-600">{d}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
