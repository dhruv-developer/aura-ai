"use client";

import { useEffect, useRef, useState } from "react";

/** Two independent display flags for the multi-agent answers:
 * - timeline: watch the agents work live (split, drafts, reviews, revisions, follow-ups)
 * - summary: the reviewed tree under each finished answer
 * Defaults come from NEXT_PUBLIC_AURA_AGENT_TIMELINE / _SUMMARY (on unless "false");
 * the user's choice is remembered per browser. */
export interface AgentFlags {
  timeline: boolean;
  summary: boolean;
}

const KEYS = { timeline: "aura_agent_timeline", summary: "aura_agent_summary" } as const;
const DEFAULTS: AgentFlags = {
  timeline: process.env.NEXT_PUBLIC_AURA_AGENT_TIMELINE !== "false",
  summary: process.env.NEXT_PUBLIC_AURA_AGENT_SUMMARY !== "false",
};

export function useAgentFlags(): [AgentFlags, (k: keyof AgentFlags, v: boolean) => void] {
  const [flags, setFlags] = useState<AgentFlags>(DEFAULTS);
  useEffect(() => {
    const read = (k: keyof AgentFlags) => {
      const v = localStorage.getItem(KEYS[k]);
      return v === null ? DEFAULTS[k] : v === "true";
    };
    setFlags({ timeline: read("timeline"), summary: read("summary") });
  }, []);
  return [
    flags,
    (k, v) => {
      localStorage.setItem(KEYS[k], String(v));
      setFlags((f) => ({ ...f, [k]: v }));
    },
  ];
}

export function AgentFlagsMenu({ flags, onChange }: { flags: AgentFlags; onChange: (k: keyof AgentFlags, v: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="rounded-md border border-zinc-200 px-2 py-1 text-sm dark:border-zinc-700"
        title="What to show about the agents"
      >
        Agents ▾
      </button>
      {open ? (
        <div className="absolute right-0 z-30 mt-1 w-64 space-y-2 rounded-md border border-zinc-200 bg-white p-3 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900">
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={flags.timeline} onChange={(e) => onChange("timeline", e.target.checked)} className="mt-1" />
            <span>
              <span className="font-medium">Live agent timeline</span>
              <span className="block text-xs text-zinc-500">Watch drafting, reviews and revisions as they happen.</span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={flags.summary} onChange={(e) => onChange("summary", e.target.checked)} className="mt-1" />
            <span>
              <span className="font-medium">Agent summary</span>
              <span className="block text-xs text-zinc-500">The reviewed question tree under each answer.</span>
            </span>
          </label>
        </div>
      ) : null}
    </div>
  );
}
