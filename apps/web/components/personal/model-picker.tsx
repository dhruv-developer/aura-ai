"use client";

import { models as modelsApi, type ModelInfo } from "@aura/api-client";
import { useEffect, useState } from "react";

const PROVIDER_LABEL: Record<string, string> = {
  ollama: "On this computer · free",
  groq: "Groq",
  anthropic: "Anthropic",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  stub: "Test models",
};

/** "Let AURA choose" (the default) or one specific model the account can call now. */
export function ModelPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  const [list, setList] = useState<ModelInfo[]>([]);

  useEffect(() => {
    modelsApi
      .list()
      .then((r) => setList(r.data.filter((m) => m.aura?.usable)))
      .catch(() => setList([]));
  }, []);

  const byProvider = new Map<string, ModelInfo[]>();
  for (const m of list) {
    const group = byProvider.get(m.owned_by) ?? [];
    group.push(m);
    byProvider.set(m.owned_by, group);
  }
  const groups = [...byProvider.entries()];

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Model"
      title="Model"
      className="max-w-[14rem] rounded-md border border-zinc-200 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-900"
    >
      <option value="">✦ AURA chooses (recommended)</option>
      {groups.map(([provider, items]) => {
        return (
          <optgroup key={provider} label={PROVIDER_LABEL[provider] ?? provider}>
            {items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.aura?.display_name ?? m.id} ({m.aura?.tier})
              </option>
            ))}
          </optgroup>
        );
      })}
    </select>
  );
}
