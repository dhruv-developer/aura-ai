"use client";

import { admin } from "@aura/api-client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

const nav = [
  ["/", "Overview"],
  ["/accounts", "Accounts"],
  ["/economics", "Unit economics"],
  ["/leads", "Leads & pilots"],
  ["/audit", "Audit log"],
] as const;

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  return (
    <div className="flex min-h-screen">
      <aside className="w-56 border-r border-zinc-100 bg-zinc-50 p-4">
        <div className="mb-6 text-lg font-bold">AURA Admin</div>
        <nav className="space-y-1 text-sm">
          {nav.map(([href, label]) => (
            <Link key={href} href={href} className={`block rounded-md px-3 py-2 ${path === href ? "bg-white font-medium shadow-sm" : "text-zinc-600"}`}>
              {label}
            </Link>
          ))}
        </nav>
        <button className="mt-8 text-sm text-zinc-500" onClick={() => admin.logout().then(() => router.replace("/totp"))}>
          End admin session
        </button>
      </aside>
      <main className="flex-1 p-8">{children}</main>
    </div>
  );
}
