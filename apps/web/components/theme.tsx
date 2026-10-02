"use client";

import { useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";
const KEY = "aura_theme";
const EVENT = "aura-theme"; // keeps every toggle on the page in sync

/** Runs before first paint (inlined in the root layout) so there is no light flash. */
export const THEME_BOOT_SCRIPT = `(function(){try{var t=localStorage.getItem("${KEY}")||"system";var d=t==="dark"||(t==="system"&&matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.classList.toggle("dark",d)}catch(e){}})();`;

function systemDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function apply(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && systemDark()));
}

export function useTheme(): { theme: Theme; dark: boolean; setTheme: (t: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>("system");
  const [dark, setDark] = useState(false);

  useEffect(() => {
    const read = () => {
      const t = (localStorage.getItem(KEY) as Theme | null) ?? "system";
      setThemeState(t);
      setDark(document.documentElement.classList.contains("dark"));
    };
    read();
    window.addEventListener(EVENT, read);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onSystem = () => {
      if ((localStorage.getItem(KEY) ?? "system") === "system") {
        apply("system");
        read();
      }
    };
    mq.addEventListener("change", onSystem);
    return () => {
      window.removeEventListener(EVENT, read);
      mq.removeEventListener("change", onSystem);
    };
  }, []);

  return {
    theme,
    dark,
    setTheme: (t: Theme) => {
      localStorage.setItem(KEY, t);
      apply(t);
      window.dispatchEvent(new Event(EVENT));
    },
  };
}

/** One click: switch between light and dark (shown in headers and navigation). */
export function ThemeSwitch({ className = "" }: { className?: string }) {
  const { dark, setTheme } = useTheme();
  const label = dark ? "Switch to light mode" : "Switch to dark mode";
  return (
    <button
      type="button"
      onClick={() => setTheme(dark ? "light" : "dark")}
      aria-label={label}
      title={label}
      className={`inline-flex h-8 w-8 items-center justify-center rounded-md border border-zinc-200 text-base hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800 ${className}`}
    >
      <span aria-hidden>{dark ? "☀" : "☾"}</span>
    </button>
  );
}

const OPTIONS: { value: Theme; label: string; icon: string }[] = [
  { value: "light", label: "Light", icon: "☀" },
  { value: "dark", label: "Dark", icon: "☾" },
  { value: "system", label: "System", icon: "◐" },
];

/** Light / Dark / System (settings-style control in the app sidebar). */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  return (
    <div className="flex rounded-md bg-zinc-100 p-0.5 text-xs dark:bg-zinc-800" role="radiogroup" aria-label="Theme">
      {OPTIONS.map((o) => (
        <button
          key={o.value}
          role="radio"
          aria-checked={theme === o.value}
          title={o.label}
          onClick={() => setTheme(o.value)}
          className={`flex-1 rounded px-2 py-1 ${theme === o.value ? "bg-white shadow-sm dark:bg-zinc-700" : "text-zinc-500"}`}
        >
          <span aria-hidden>{o.icon}</span> {o.label}
        </button>
      ))}
    </div>
  );
}
