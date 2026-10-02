"use client";

import { useParams } from "next/navigation";

import { ChatView } from "@/components/personal/chat-view";

export default function Conversation() {
  const { id } = useParams<{ id: string }>();
  return <ChatView key={id} initialId={id} />;
}
