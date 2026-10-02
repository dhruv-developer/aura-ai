"use client";

import { auth } from "@aura/api-client";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { ErrorBox, errorMessage } from "@/components/ui";

function Verify() {
  const params = useSearchParams();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // links are single-use: never verify twice (StrictMode)
    started.current = true;
    const token = params.get("token");
    if (!token) {
      setError("This sign-in link is missing its token.");
      return;
    }
    auth
      .verify(token)
      .then(() => router.replace("/dashboard"))
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [params, router]);

  return (
    <main className="mx-auto mt-24 max-w-sm px-6">
      {error ? <ErrorBox error={error} /> : <p className="text-zinc-600">Signing you in…</p>}
    </main>
  );
}

export default function VerifyPage() {
  return (
    <Suspense>
      <Verify />
    </Suspense>
  );
}
