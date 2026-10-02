"use client";

import { auth, orgs } from "@aura/api-client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ErrorBox, errorMessage, Loading, PageTitle, useAsync } from "@/components/ui";

export default function Dashboard() {
  const router = useRouter();
  const me = useAsync(() => auth.me(), []);
  const [error, setError] = useState<string | null>(null);

  const unauthenticated = me.error?.toLowerCase().includes("authentication") ?? false;
  useEffect(() => {
    if (unauthenticated) router.replace("/login");
  }, [unauthenticated, router]);

  async function createOrg(form: FormData) {
    setError(null);
    try {
      const org = await orgs.create(String(form.get("name") ?? ""));
      router.push(`/org/${org.account_id}`);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <PageTitle title="Your workspaces">
        <button className="btn-secondary" onClick={() => auth.logout().then(() => router.replace("/login"))}>
          Sign out
        </button>
      </PageTitle>
      <ErrorBox error={me.error} />
      {me.loading ? <Loading /> : null}
      <div className="grid gap-4 md:grid-cols-2">
        {me.data?.accounts.map((a) => (
          <Link
            key={a.account_id}
            href={a.kind === "organization" ? `/org/${a.account_id}` : "/app"}
            className="card hover:border-indigo-300"
          >
            <div className="text-xs uppercase text-zinc-400">{a.kind === "organization" ? "Organization" : "Personal"}</div>
            <div className="mt-1 text-lg font-semibold">{a.name}</div>
            <div className="text-sm text-zinc-500">Role: {a.role}</div>
          </Link>
        ))}
      </div>
      <form action={createOrg} className="card mt-8 flex gap-3">
        <input className="input" name="name" placeholder="New organization name" minLength={2} required />
        <button className="btn-primary whitespace-nowrap">Create organization</button>
      </form>
      <ErrorBox error={error} />
    </main>
  );
}
