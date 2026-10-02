"use client";

import { auth } from "@aura/api-client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ErrorBox, errorMessage } from "@/components/ui";

export default function Login() {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Invite links (?ref=CODE): remember the code; the personal app redeems it after sign-in.
  useEffect(() => {
    const ref = new URLSearchParams(window.location.search).get("ref");
    if (ref && /^[A-Za-z0-9]{4,16}$/.test(ref)) localStorage.setItem("aura_ref", ref);
  }, []);

  async function submit(form: FormData) {
    setError(null);
    try {
      await auth.requestLink(String(form.get("email") ?? ""));
      setSent(true);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <main className="mx-auto mt-24 max-w-sm px-6">
      <Link href="/" className="text-lg font-bold">AURA</Link>
      <h1 className="mt-8 text-2xl font-semibold">Sign in</h1>
      {sent ? (
        <p className="mt-4 text-zinc-600">Check your inbox: we sent you a sign-in link that expires in 15 minutes.</p>
      ) : (
        <form action={submit} className="mt-6 space-y-4">
          <div>
            <label className="label" htmlFor="email">Email</label>
            <input className="input" id="email" name="email" type="email" autoComplete="email" required />
          </div>
          <ErrorBox error={error} />
          <button className="btn-primary w-full">Email me a sign-in link</button>
          <a className="btn-secondary w-full" href="/v1/auth/google/start?return_to=/dashboard">Continue with Google</a>
        </form>
      )}
    </main>
  );
}
