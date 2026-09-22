"use client";

// 工具调用/结果卡片。

import type { ToolCallRecord } from "@/lib/chat";
import { useState } from "react";

function argPreview(args: Record<string, unknown>): string {
  const s = JSON.stringify(args, null, 0);
  return s.length > 120 ? `${s.slice(0, 120)}…` : s;
}

export default function ToolCallCard({ call }: { call: ToolCallRecord }) {
  const [open, setOpen] = useState(false);
  const isSubagent = call.kind === "subagent";
  const done = call.status !== undefined;

  return (
    <div className="mt-1.5 rounded-lg border border-slate-800 bg-slate-900/40 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-slate-300 hover:text-slate-100"
      >
        <span>{isSubagent ? "🤖" : "🔧"}</span>
        <span className="font-mono text-cyan-300">{call.tool}</span>
        <span
          className={`ml-auto rounded px-1.5 py-0.5 text-[10px] ${
            done ? "bg-green-500/15 text-green-400" : "bg-amber-500/15 text-amber-300"
          }`}
        >
          {done ? call.status : "运行中…"}
        </span>
      </button>
      {open && (
        <div className="border-t border-slate-800 px-3 py-1.5 font-mono text-[11px] text-slate-500">
          {argPreview(call.args)}
        </div>
      )}
    </div>
  );
}
