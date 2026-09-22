"use client";

// 消息滚动列表，自动滚到底部。

import type { ChatMessage } from "@/lib/chat";
import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";

export default function MessageList({
  messages,
  streaming,
}: {
  messages: ChatMessage[];
  streaming: boolean;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streaming]);

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-600">
        <div className="text-5xl">🐝</div>
        <div className="text-sm">输入一个股票或问题，开始投资分析</div>
        <div className="text-xs text-slate-700">例如：帮我分析一下宁德时代的投资价值</div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} streaming={streaming} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
