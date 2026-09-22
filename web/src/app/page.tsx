"use client";

// 主聊天页：组合 PipelineStepper + MessageList + Composer + FlagsPanel。

import Composer from "@/components/chat/Composer";
import MessageList from "@/components/chat/MessageList";
import FlagsPanel from "@/components/controls/FlagsPanel";
import PipelineStepper from "@/components/pipeline/PipelineStepper";
import { useChatStream } from "@/hooks/useChatStream";

export default function Home() {
  const { messages, stages, isStreaming, flags, setFlags, send, clear } = useChatStream();

  return (
    <div className="mx-auto flex h-screen max-w-4xl flex-col gap-3 px-4 py-4">
      <header className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-2xl">🐝</span>
          <h1 className="text-lg font-bold text-cyan-300">AlphaBee 投资分析</h1>
        </div>
        <div className="flex items-center gap-3">
          <FlagsPanel flags={flags} onChange={setFlags} />
          <button
            type="button"
            onClick={clear}
            disabled={isStreaming}
            className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-400 hover:text-slate-200 disabled:opacity-40"
          >
            清空会话
          </button>
        </div>
      </header>

      <PipelineStepper stages={stages} />

      <main className="flex-1 overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/40 p-4">
        <MessageList messages={messages} streaming={isStreaming} />
      </main>

      <footer className="pb-1">
        <Composer disabled={isStreaming} onSend={send} />
      </footer>
    </div>
  );
}
