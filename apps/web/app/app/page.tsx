"use client";

import { useChatList } from "@/components/personal/chat-list";
import { ChatView } from "@/components/personal/chat-view";

/** A new chat. The conversation is created on the first message; the URL then becomes
 * /app/c/{id} without remounting, so the first answer keeps streaming. */
export default function NewChat() {
  const { newChatNonce } = useChatList();
  return <ChatView key={newChatNonce} initialId={null} />;
}
