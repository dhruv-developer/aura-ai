"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { ThemeSwitch } from "@/components/theme";

const tabs = [
  ["", "Overview"],
  ["/xray", "X-Ray"],
  ["/shadow", "Shadow runs"],
  ["/runs", "Graph runs"],
  ["/api-keys", "API keys"],
  ["/providers", "Provider keys"],
  ["/billing", "Billing"],
] as const;

export default function OrgLayout({ children }: { children: ReactNode }) {
  const { accountId } = useParams<{ accountId: string }>();
  const path = usePathname();
  const base = `/org/${accountId}`;
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 border-r border-zinc-100 p-4">
        <div className="mb-6 flex items-center justify-between">
          <Link href="/dashboard" className="text-lg font-bold">AURA</Link>
          <ThemeSwitch />
        </div>
        <nav className="space-y-1 text-sm">
          {tabs.map(([href, label]) => {
            const active = href === "" ? path === base : path.startsWith(base + href);
            return (
              <Link
                key={label}
                href={base + href}
                className={`block rounded-md px-3 py-2 ${active ? "bg-indigo-50 font-medium text-indigo-700" : "text-zinc-600 hover:bg-zinc-50"}`}
              >
                {label}
              </Link>
            );
          })}
        </nav>
      </aside>
      <main className="flex-1 p-8">{children}</main>
    </div>
  );
}
