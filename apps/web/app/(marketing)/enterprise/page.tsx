const features = [
  "Private deployment, VPC and regional runtimes (India region)",
  "SSO, SCIM and advanced RBAC",
  "Customer-managed encryption keys",
  "Data residency and PII policies enforced at routing time",
  "Audit export and dedicated support with SLA",
  "Performance-linked pricing on verified savings",
];

export default function Enterprise() {
  return (
    <section className="py-24">
      <h1 className="max-w-3xl text-5xl font-bold tracking-tight">
        The control layer that continuously optimizes how your AI applications execute.
      </h1>
      <ul className="mt-10 grid gap-3 md:grid-cols-2">
        {features.map((f) => (
          <li key={f} className="card">{f}</li>
        ))}
      </ul>
      <a href="mailto:sales@aura.ai?subject=Enterprise" className="btn-primary mt-10">Talk to sales</a>
    </section>
  );
}
