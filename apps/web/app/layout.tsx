import type { Metadata } from "next";
import type { ReactNode } from "react";

import { THEME_BOOT_SCRIPT } from "@/components/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: "AURA: AI Execution Optimization",
  description: "AURA decides how every AI task should be executed: the right context, models and tools at the quality, cost and latency you need.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // The theme script sets the `dark` class before React hydrates.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
