"use client";

// 模型"思考"折叠块。

import type { ThinkingRecord } from "@/lib/chat";
import { useState } from "react";

export default function ThinkingBlock({ items }: { items: ThinkingRecord[] }) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;

  return (
    <div className="mt-2 rounded-lg border border-slate-800 bg-slate-900/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-xs text-slate-400 hover:text-slate-200"
      >
        <span>💭</span>
        <span>思考过程（{items.length}）</span>
        <span className="ml-auto">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <div className="max-h-64 overflow-y-auto border-t border-slate-800 px-3 py-2">
          {items.map((t, i) => (
            <div key={i} className="mb-1 text-xs text-slate-400">
              <span className="text-slate-600">[{t.agent}]</span> {t.text}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
