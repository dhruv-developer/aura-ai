"use client";

import { admin, ApiError } from "@aura/api-client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { ErrorBox, errorMessage } from "@/components/ui";

export default function Totp() {
  const router = useRouter();
  const [enrollment, setEnrollment] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function enroll() {
    setError(null);
    try {
      setEnrollment(await admin.enroll());
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? "Already enrolled: enter your code." : errorMessage(e));
    }
  }

  async function verify(form: FormData) {
    setError(null);
    try {
      await admin.verify(String(form.get("code") ?? ""));
      router.replace("/");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <main className="mx-auto mt-24 max-w-sm px-6">
      <h1 className="text-2xl font-semibold">Two-factor verification</h1>
      <form action={verify} className="mt-6 space-y-4">
        <input className="input text-center tracking-widest" name="code" inputMode="numeric" autoComplete="one-time-code" placeholder="123456" maxLength={6} required />
        <ErrorBox error={error} />
        <button className="btn-primary w-full">Verify</button>
      </form>
      <button className="mt-6 text-sm text-indigo-600" onClick={enroll}>First time? Set up your authenticator</button>
      {enrollment ? (
        <div className="card mt-4 text-sm">
          <p>Add this secret to your authenticator app, then enter the 6-digit code above.</p>
          <code className="mt-2 block break-all">{enrollment.secret}</code>
          <a className="mt-2 block text-indigo-600" href={enrollment.otpauth_uri}>Open in authenticator</a>
        </div>
      ) : null}
    </main>
  );
}
