"use client";

import { personal } from "@aura/api-client";
import { useState, type FormEvent } from "react";

import { ErrorBox, Loading, PageTitle, errorMessage, useAsync } from "@/components/ui";

/** Spec §17: personal memory is visible and editable, and forgetting is real deletion. */
export default function Memory() {
  const { data, error, loading, reload } = useAsync(() => personal.memories(), []);
  const [text, setText] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  async function act(fn: () => Promise<unknown>) {
    setActionError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setActionError(errorMessage(e));
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    await act(() => personal.addMemory(text.trim()));
    setText("");
  }

  return (
    <div className="max-w-2xl">
      <PageTitle title="Memory">
        <button
          className="btn-danger"
          onClick={() => {
            if (confirm("Permanently delete everything AURA remembers about you?")) void act(() => personal.forgetAll());
          }}
        >
          Forget everything
        </button>
      </PageTitle>
      <p className="mb-4 text-sm text-zinc-600">
        Preferences and facts AURA uses to personalise answers (language, style, budget). Only you can see them.
      </p>
      <form onSubmit={add} className="mb-6 flex gap-2">
        <input
          className="input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. Reply in Hindi; keep answers short"
          aria-label="New memory"
        />
        <button className="btn-primary">Remember</button>
      </form>
      <ErrorBox error={error ?? actionError} />
      {loading ? <Loading /> : null}
      <ul className="divide-y divide-zinc-100">
        {data?.data.map((m) => (
          <li key={m.id} className="flex items-center justify-between py-3 text-sm">
            <span>{m.content}</span>
            <button className="text-red-600 hover:underline" onClick={() => act(() => personal.deleteMemory(m.id))}>
              Delete
            </button>
          </li>
        ))}
        {data && data.data.length === 0 ? <li className="py-3 text-sm text-zinc-500">Nothing yet.</li> : null}
      </ul>
    </div>
  );
}
