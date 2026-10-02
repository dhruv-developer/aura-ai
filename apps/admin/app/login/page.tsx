"use client";

import { auth } from "@aura/api-client";
import { useState } from "react";
import { ErrorBox, errorMessage } from "@/components/ui";

export default function AdminLogin() {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(form: FormData) {
    setError(null);
    try {
      await auth.requestLink(String(form.get("email") ?? ""), "admin");
      setSent(true);
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <main className="mx-auto mt-24 max-w-sm px-6">
      <h1 className="text-2xl font-semibold">AURA Superadmin</h1>
      {sent ? (
        <p className="mt-4 text-zinc-600">Check your inbox for the sign-in link.</p>
      ) : (
        <form action={submit} className="mt-6 space-y-4">
          <input className="input" name="email" type="email" placeholder="you@aura.ai" required />
          <ErrorBox error={error} />
          <button className="btn-primary w-full">Send sign-in link</button>
        </form>
      )}
    </main>
  );
}
