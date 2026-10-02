import type { ReactNode } from "react";
import { SiteFooter, SiteNav } from "@/components/site-nav";

export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <SiteNav />
      <main className="mx-auto max-w-6xl px-6">{children}</main>
      <SiteFooter />
    </>
  );
}
