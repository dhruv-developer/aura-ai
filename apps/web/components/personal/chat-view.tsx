"use client";

import { ApiError, chat, personal, type AgentEvent, type ChatMessage, type Mode } from "@aura/api-client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

import { useChatList } from "@/components/personal/chat-list";
import { usePersonal } from "@/components/personal/context";
import { Markdown } from "@/components/personal/markdown";
import { ModelPicker } from "@/components/personal/model-picker";
import { OptimizedPanel } from "@/components/personal/optimized-panel";
import { SubqueryPanel } from "@/components/personal/subquery-panel";
import { AgentFlagsMenu, useAgentFlags } from "@/components/personal/agent-flags";
import { AgentTimeline } from "@/components/personal/agent-timeline";
import { AgentTreePanel } from "@/components/personal/agent-tree-panel";
import { ThemeSwitch } from "@/components/theme";
import { errorMessage } from "@/components/ui";

const MODES: { value: Mode; label: string; hint: string }[] = [
  { value: "auto", label: "Auto", hint: "AURA picks the cheapest model that does the job, and splits multi-part questions" },
  { value: "fast", label: "Fast", hint: "Quickest, cheapest answers" },
  { value: "balanced", label: "Balanced", hint: "Good answers at a fair price" },
  { value: "best", label: "Best", hint: "Strongest models, highest cost" },
];

const SUGGESTIONS = [
  "Explain how compound interest works with an example",
  "What is RAG? How does it reduce hallucinations? When should I not use it?",
  "Write a polite email asking my landlord to fix the water heater",
  "1. Best time to visit Kerala\n2. A 5-day budget itinerary\n3. What to pack",
];

interface Streaming {
  text: string;
  plan: string[] | null;
  agents: AgentEvent[];
}

export function ChatView({ initialId }: { initialId: string | null }) {
  const { account } = usePersonal();
  const list = useChatList();
  const [convId, setConvId] = useState<string | null>(initialId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [mode, setMode] = useState<Mode>("auto");
  const [model, setModel] = useState(""); // "" = AURA chooses
  const [flags, setFlag] = useAgentFlags();
  const [loading, setLoading] = useState(initialId !== null);
  // Reopened a chat whose answer is still being written on the server.
  const [pending, setPending] = useState(false);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState<Streaming | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outOfCredits, setOutOfCredits] = useState(false);
  const [shared, setShared] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement | null>(null);
  const box = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!initialId) return;
    let live = true;
    chat
      .get(initialId)
      .then(async (c) => {
        if (!live) return;
        setMessages(c.messages);
        setMode(c.mode);
        if (c.messages.at(-1)?.role === "user" && (await chat.status(initialId)).answering) {
          setPending(true);
        }
      })
      .catch((e: unknown) => live && setError(errorMessage(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [initialId]);

  // Answers keep running on the server when you leave a chat; poll until it lands.
  useEffect(() => {
    if (!pending || !convId) return;
    const timer = setInterval(async () => {
      try {
        const [c, s] = await Promise.all([chat.get(convId), chat.status(convId)]);
        setMessages(c.messages);
        if (!s.answering) {
          setPending(false);
          void list.reload();
        }
      } catch {
        setPending(false);
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [pending, convId, list]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, streaming?.text, streaming?.plan]);

  useEffect(() => () => abort.current?.abort(), []);

  const send = useCallback(
    async (raw: string) => {
      const content = raw.trim();
      if (!content || streaming || pending) return;
      setError(null);
      setOutOfCredits(false);
      setInput("");
      let id = convId;
      try {
        if (!id) {
          const conv = await chat.create(mode);
          id = conv.id;
          setConvId(id);
          list.upsert(conv);
          // Update the URL without remounting, so the stream below keeps running.
          window.history.replaceState(null, "", `/app/c/${id}`);
        }
      } catch (e) {
        setError(errorMessage(e));
        return;
      }
      const temp: ChatMessage = { id: `tmp-${Date.now()}`, role: "user", content, aura: null, created_at: new Date().toISOString() };
      setMessages((m) => [...m, temp]);
      setStreaming({ text: "", plan: null, agents: [] });
      const ctrl = new AbortController();
      abort.current = ctrl;
      try {
        await chat.send(
          id,
          { content, mode, ...(model ? { model } : {}) },
          (ev) => {
            if (ev.type === "user") setMessages((m) => m.map((x) => (x.id === temp.id ? ev.message : x)));
            else if (ev.type === "plan") setStreaming((s) => (s ? { ...s, plan: ev.subqueries } : s));
            else if (ev.type === "agent") setStreaming((s) => (s ? { ...s, agents: [...s.agents, ev.event] } : s));
            else if (ev.type === "delta") setStreaming((s) => (s ? { ...s, text: s.text + ev.text } : s));
            else if (ev.type === "done") {
              setMessages((m) => [...m, ev.message]);
              setStreaming(null);
            } else if (ev.type === "error") {
              setError(ev.problem.detail ?? ev.problem.title);
              if (ev.problem.status === 402) setOutOfCredits(true);
            }
          },
          ctrl.signal,
        );
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          if (e instanceof ApiError && e.status === 402) setOutOfCredits(true);
          setError(errorMessage(e));
        }
      } finally {
        abort.current = null;
        setStreaming((s) => {
          // Stopped or failed mid-answer: keep what arrived, marked as incomplete.
          if (s && s.text) {
            setMessages((m) => [
              ...m,
              { id: `partial-${Date.now()}`, role: "assistant", content: `${s.text}\n\n_(stopped)_`, aura: null, created_at: new Date().toISOString() },
            ]);
          }
          return null;
        });
        void list.reload(); // new title and ordering
        box.current?.focus();
      }
    },
    [convId, list, mode, model, pending, streaming],
  );

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(input);
    }
  }

  async function share(m: ChatMessage) {
    if (m.aura?.kind !== "single") return;
    try {
      const card = await personal.share(account.account_id, m.aura.execution_id);
      const url = `${window.location.origin}/share/${card.id}`;
      await navigator.clipboard?.writeText(url).catch(() => undefined);
      setShared((s) => ({ ...s, [m.id]: url }));
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function copy(m: ChatMessage) {
    await navigator.clipboard?.writeText(m.content).catch(() => undefined);
    setCopied(m.id);
    setTimeout(() => setCopied(null), 1500);
  }

  const empty = !loading && messages.length === 0 && !streaming;

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b border-zinc-100 px-4 py-2 dark:border-zinc-800">
        <div
          className={`flex gap-1 rounded-lg bg-zinc-100 p-1 dark:bg-zinc-800 ${model ? "opacity-40" : ""}`}
          role="radiogroup"
          aria-label="Mode"
          title={model ? "Modes apply when AURA chooses the model" : undefined}
        >
          {MODES.map((m) => (
            <button
              key={m.value}
              role="radio"
              aria-checked={mode === m.value}
              disabled={Boolean(model)}
              title={m.hint}
              onClick={() => setMode(m.value)}
              className={`rounded-md px-3 py-1 text-sm ${mode === m.value ? "bg-white font-medium shadow-sm dark:bg-zinc-700" : "text-zinc-600 dark:text-zinc-400"}`}
            >
              {m.label}
            </button>
          ))}
        </div>
        <ModelPicker value={model} onChange={setModel} />
        <AgentFlagsMenu flags={flags} onChange={setFlag} />
        <span className="hidden flex-1 truncate text-xs text-zinc-400 lg:block">
          {model ? "Every message goes to the model you picked." : MODES.find((m) => m.value === mode)?.hint}
        </span>
        <ThemeSwitch className="ml-auto" />
      </header>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-6 px-4 py-6">
          {loading ? <div className="text-sm text-zinc-400">Loading…</div> : null}
          {empty ? (
            <div className="mt-16 text-center">
              <h1 className="text-3xl font-semibold">What can I help with?</h1>
              <p className="mt-2 text-sm text-zinc-500">
                Auto mode routes every question to the cheapest model that can answer it well, and splits multi-part questions.
              </p>
              <div className="mx-auto mt-8 grid max-w-2xl gap-2 sm:grid-cols-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => void send(s)}
                    className="whitespace-pre-line rounded-xl border border-zinc-200 p-3 text-left text-sm text-zinc-600 hover:bg-zinc-50 dark:border-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-900"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-zinc-100 px-4 py-2 text-sm dark:bg-zinc-800">{m.content}</div>
              </div>
            ) : (
              <div key={m.id} className="text-sm">
                <Markdown text={m.content} />
                {m.aura?.kind === "single" ? <OptimizedPanel aura={m.aura} /> : null}
                {m.aura?.kind === "subqueries" ? (
                  <SubqueryPanel aura={m.aura} questions={m.trace && "subqueries" in m.trace ? m.trace.subqueries : []} />
                ) : null}
                {m.aura?.kind === "agents" && flags.summary ? <AgentTreePanel aura={m.aura} trace={m.trace} /> : null}
                <div className="mt-1 flex gap-3 text-xs text-zinc-400">
                  <button className="hover:text-zinc-700 dark:hover:text-zinc-200" onClick={() => void copy(m)}>
                    {copied === m.id ? "Copied" : "Copy"}
                  </button>
                  {m.aura?.kind === "single" ? (
                    shared[m.id] ? (
                      <span className="text-emerald-600">Link copied: {shared[m.id]}</span>
                    ) : (
                      <button className="hover:text-zinc-700 dark:hover:text-zinc-200" onClick={() => void share(m)}>
                        Share how AURA optimized this
                      </button>
                    )
                  ) : null}
                </div>
              </div>
            ),
          )}

          {streaming ? (
            <div className="text-sm">
              {streaming.plan ? (
                <div className="mb-3 rounded-md border border-indigo-200 bg-indigo-50 p-3 text-xs dark:border-indigo-900">
                  <div className="font-medium text-indigo-700">Answering {streaming.plan.length} sub-questions in parallel…</div>
                  <ol className="mt-1 list-decimal pl-5 text-zinc-600">
                    {streaming.plan.map((q) => (
                      <li key={q}>{q}</li>
                    ))}
                  </ol>
                </div>
              ) : null}
              {streaming.agents.length && flags.timeline ? <AgentTimeline events={streaming.agents} /> : null}
              {streaming.text ? (
                <Markdown text={streaming.text} />
              ) : (
                <div className="animate-pulse text-zinc-400">
                  {streaming.agents.length ? "Drafting and reviewing…" : "Thinking…"}
                </div>
              )}
            </div>
          ) : null}

          {pending ? (
            <div className="animate-pulse text-sm text-zinc-400">Still answering… (this continues even if you leave the chat)</div>
          ) : null}

          {error ? (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900">
              {error}
              {outOfCredits ? (
                <div className="mt-1">
                  <Link href="/app/usage" className="underline">Buy credits</Link>,{" "}
                  <Link href="/app/subscription" className="underline">upgrade</Link>, or add your own provider key.
                </div>
              ) : null}
            </div>
          ) : null}
          <div ref={bottom} />
        </div>
      </div>

      <div className="border-t border-zinc-100 px-4 py-3 dark:border-zinc-800">
        <form
          className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl border border-zinc-300 bg-white p-2 dark:border-zinc-700 dark:bg-zinc-900"
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
        >
          <textarea
            ref={box}
            rows={1}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`;
            }}
            onKeyDown={onKey}
            placeholder="Message AURA"
            aria-label="Message"
            className="max-h-[200px] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none"
          />
          {streaming ? (
            <button
              type="button"
              onClick={() => {
                if (convId) void chat.stop(convId).catch(() => undefined);
                abort.current?.abort();
              }}
              className="btn-secondary px-3 py-1.5"
              aria-label="Stop"
            >
              ■ Stop
            </button>
          ) : (
            <button className="btn-primary px-3 py-1.5" disabled={!input.trim()} aria-label="Send">
              Send
            </button>
          )}
        </form>
        <p className="mt-1.5 text-center text-[11px] text-zinc-400">AURA can make mistakes. Check important information.</p>
      </div>
    </div>
  );
}
