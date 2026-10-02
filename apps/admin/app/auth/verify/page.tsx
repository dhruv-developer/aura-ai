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
    if (started.current) return;
    started.current = true;
    const token = params.get("token");
    if (!token) {
      setError("Missing token.");
      return;
    }
    auth
      .verify(token)
      .then(() => router.replace("/totp"))
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [params, router]);
  return <main className="mx-auto mt-24 max-w-sm px-6">{error ? <ErrorBox error={error} /> : "Signing in…"}</main>;
}

export default function VerifyPage() {
  return (
    <Suspense>
      <Verify />
    </Suspense>
  );
}
