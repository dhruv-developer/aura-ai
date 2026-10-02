"use client";

import { chat, type Conversation } from "@aura/api-client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

interface ChatList {
  conversations: Conversation[] | null;
  reload: () => Promise<void>;
  upsert: (c: Conversation) => void;
  remove: (id: string) => void;
  /** Bumped by "New chat" so the chat view starts fresh even on the same URL. */
  newChatNonce: number;
  newChat: () => void;
}

const Ctx = createContext<ChatList | null>(null);

export function useChatList(): ChatList {
  const v = useContext(Ctx);
  if (!v) throw new Error("useChatList must be used inside ChatListProvider");
  return v;
}

export function ChatListProvider({ children }: { children: ReactNode }) {
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [newChatNonce, setNonce] = useState(0);

  const reload = useCallback(async () => {
    try {
      setConversations((await chat.list()).data);
    } catch {
      setConversations((c) => c ?? []);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const upsert = useCallback((c: Conversation) => {
    setConversations((list) => [c, ...(list ?? []).filter((x) => x.id !== c.id)]);
  }, []);
  const remove = useCallback((id: string) => {
    setConversations((list) => (list ?? []).filter((x) => x.id !== id));
  }, []);
  const newChat = useCallback(() => setNonce((n) => n + 1), []);

  return (
    <Ctx.Provider value={{ conversations, reload, upsert, remove, newChatNonce, newChat }}>{children}</Ctx.Provider>
  );
}

/** Group like ChatGPT: Today, Yesterday, Previous 7 days, Older. */
export function groupByRecency(list: Conversation[]): [string, Conversation[]][] {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const day = 86_400_000;
  const groups: Record<string, Conversation[]> = { Today: [], Yesterday: [], "Previous 7 days": [], Older: [] };
  for (const c of list) {
    const t = new Date(c.updated_at).getTime();
    const g = t >= start.getTime() ? "Today" : t >= start.getTime() - day ? "Yesterday" : t >= start.getTime() - 7 * day ? "Previous 7 days" : "Older";
    groups[g]!.push(c);
  }
  return Object.entries(groups).filter(([, items]) => items.length > 0);
}
