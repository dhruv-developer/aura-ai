import Link from "next/link";

import { ThemeSwitch } from "@/components/theme";

export function SiteNav() {
  return (
    <header className="border-b border-zinc-100">
      <nav className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <Link href="/" className="text-lg font-bold tracking-tight">
          AURA
        </Link>
        <div className="flex items-center gap-6 text-sm text-zinc-600">
          <Link href="/for-individuals">For individuals</Link>
          <Link href="/for-startups">For startups</Link>
          <Link href="/enterprise">Enterprise</Link>
          <Link href="/developers">Developers</Link>
          <Link href="/pricing">Pricing</Link>
          <Link href="/security">Security</Link>
          <ThemeSwitch />
          <Link href="/login" className="btn-secondary">
            Sign in
          </Link>
        </div>
      </nav>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="mt-24 border-t border-zinc-100 py-10 text-center text-sm text-zinc-500">
      AURA: AI Execution Optimization · <Link href="/security">Security</Link> ·{" "}
      <Link href="/docs">Docs</Link>
    </footer>
  );
}
