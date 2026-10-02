"use client";

import { ApiError, chat, personal } from "@aura/api-client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { ChatListProvider, groupByRecency, useChatList } from "@/components/personal/chat-list";
import { PersonalContext, loadPersonal, type PersonalState } from "@/components/personal/context";
import { ThemeToggle } from "@/components/theme";
import { ErrorBox, Loading, errorMessage } from "@/components/ui";

const LINKS = [
  ["/app/memory", "Memory"],
  ["/app/usage", "Usage & credits"],
  ["/app/subscription", "Subscription"],
  ["/app/invite", "Invite friends"],
] as const;

export default function PersonalLayout({ children }: { children: ReactNode }) {
  const enabled = process.env.NEXT_PUBLIC_B2C_APP === "true";
  const router = useRouter();
  const [state, setState] = useState<PersonalState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    loadPersonal()
      .then(async (loaded) => {
        const ref = localStorage.getItem("aura_ref");
        if (ref) {
          localStorage.removeItem("aura_ref");
          await personal.redeem(ref).catch(() => undefined); // best effort: invalid/expired codes are ignored
        }
        setState(loaded);
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) router.replace("/login?next=/app");
        else setError(errorMessage(e));
      });
  }, [enabled, router]);

  if (!enabled) {
    return (
      <main className="mx-auto mt-24 max-w-xl px-6 text-center">
        <h1 className="text-3xl font-bold">AURA Personal</h1>
        <p className="mt-4 text-zinc-600">
          The personal app is rolling out gradually. Your account is ready: we&apos;ll email you when
          it&apos;s available.
        </p>
        <Link href="/dashboard" className="btn-secondary mt-8">Back to workspaces</Link>
      </main>
    );
  }
  if (error) return <main className="p-8"><ErrorBox error={error} /></main>;
  if (!state) return <main className="p-8"><Loading /></main>;

  return (
    <PersonalContext.Provider value={state}>
      <ChatListProvider>
        <Shell state={state}>{children}</Shell>
      </ChatListProvider>
    </PersonalContext.Provider>
  );
}

function Shell({ state, children }: { state: PersonalState; children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const { conversations, newChat, remove, upsert } = useChatList();
  const [open, setOpen] = useState(false); // sidebar on small screens
  const chatPage = path === "/app" || path.startsWith("/app/c/");

  function startNewChat() {
    newChat();
    setOpen(false);
    router.push("/app");
  }

  async function rename(id: string, current: string) {
    const title = window.prompt("Rename chat", current)?.trim();
    if (!title || title === current) return;
    upsert(await chat.update(id, { title }));
  }

  async function del(id: string) {
    if (!window.confirm("Delete this chat? This cannot be undone.")) return;
    await chat.remove(id);
    remove(id);
    if (path === `/app/c/${id}`) startNewChat();
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <aside
        className={`${open ? "flex" : "hidden"} absolute inset-y-0 left-0 z-20 w-64 flex-col border-r border-zinc-200 bg-zinc-50 md:static md:flex dark:border-zinc-800 dark:bg-zinc-900`}
      >
        <div className="flex items-center justify-between p-3">
          <Link href="/app" onClick={startNewChat} className="text-lg font-bold">AURA</Link>
          <button className="md:hidden" onClick={() => setOpen(false)} aria-label="Close menu">✕</button>
        </div>
        <div className="px-3">
          <button onClick={startNewChat} className="btn-secondary w-full justify-start gap-2">
            <span aria-hidden>＋</span> New chat
          </button>
        </div>

        <nav className="mt-3 flex-1 overflow-y-auto px-2 text-sm" aria-label="Chats">
          {conversations === null ? <div className="px-2 text-xs text-zinc-400">Loading chats…</div> : null}
          {conversations?.length === 0 ? <div className="px-2 text-xs text-zinc-400">No chats yet.</div> : null}
          {groupByRecency(conversations ?? []).map(([group, items]) => (
            <div key={group} className="mb-3">
              <div className="px-2 pb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">{group}</div>
              {items.map((c) => {
                const active = path === `/app/c/${c.id}`;
                return (
                  <div
                    key={c.id}
                    className={`group flex items-center rounded-md ${active ? "bg-zinc-200 dark:bg-zinc-800" : "hover:bg-zinc-100 dark:hover:bg-zinc-800/60"}`}
                  >
                    <Link href={`/app/c/${c.id}`} onClick={() => setOpen(false)} className="min-w-0 flex-1 truncate px-2 py-1.5" title={c.title}>
                      {c.title}
                    </Link>
                    <div className={`flex shrink-0 gap-1 pr-1 text-xs text-zinc-400 ${active ? "" : "invisible group-hover:visible"}`}>
                      <button onClick={() => void rename(c.id, c.title)} title="Rename" aria-label={`Rename ${c.title}`} className="hover:text-zinc-700 dark:hover:text-zinc-200">✎</button>
                      <button onClick={() => void del(c.id)} title="Delete" aria-label={`Delete ${c.title}`} className="hover:text-red-600">🗑</button>
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="space-y-2 border-t border-zinc-200 p-3 text-sm dark:border-zinc-800">
          {LINKS.map(([href, label]) => (
            <Link
              key={href}
              href={href}
              onClick={() => setOpen(false)}
              className={`block rounded-md px-2 py-1 ${path.startsWith(href) ? "bg-zinc-200 font-medium dark:bg-zinc-800" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800/60"}`}
            >
              {label}
            </Link>
          ))}
          <ThemeToggle />
          <div className="flex items-center justify-between px-1 pt-1 text-xs text-zinc-500">
            <span className="truncate">{state.user.email}</span>
            {state.organizations.length > 0 ? <Link href="/dashboard" className="shrink-0 text-indigo-600">Teams →</Link> : null}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-zinc-100 px-3 py-2 md:hidden dark:border-zinc-800">
          <button onClick={() => setOpen(true)} aria-label="Open menu">☰</button>
          <span className="font-semibold">AURA</span>
        </div>
        <main className={`min-h-0 flex-1 ${chatPage ? "" : "overflow-y-auto p-8"}`}>{children}</main>
      </div>
    </div>
  );
}
