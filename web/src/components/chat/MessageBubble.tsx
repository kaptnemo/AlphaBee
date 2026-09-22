"use client";

// 单条消息气泡。

import ThinkingBlock from "@/components/pipeline/ThinkingBlock";
import ToolCallCard from "@/components/pipeline/ToolCallCard";
import ReportView from "@/components/report/ReportView";
import type { ChatMessage } from "@/lib/chat";

export default function MessageBubble({
  message,
  streaming,
}: {
  message: ChatMessage;
  streaming: boolean;
}) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-blue-600 px-4 py-2.5 text-sm text-white">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[95%] min-w-0 flex-1">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span>🐝 AlphaBee</span>
          {streaming && <span className="animate-pulse text-amber-300">分析中…</span>}
        </div>

        <div className="mt-1 rounded-2xl rounded-bl-sm border border-slate-800 bg-slate-900 px-4 py-3 text-sm text-slate-200">
          {message.thinking && message.thinking.length > 0 && (
            <ThinkingBlock items={message.thinking} />
          )}

          {message.toolCalls && message.toolCalls.length > 0 && (
            <div className="mb-2">
              {message.toolCalls.map((call, i) => (
                <ToolCallCard key={i} call={call} />
              ))}
            </div>
          )}

          {message.content && !message.report && (
            <div className="whitespace-pre-wrap">{message.content}</div>
          )}

          {message.report && <ReportView report={message.report} issues={message.issues} />}

          {streaming && !message.content && !message.report && (
            <div className="flex items-center gap-1 py-1">
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500 [animation-delay:-0.3s]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500 [animation-delay:-0.15s]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-slate-500" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
