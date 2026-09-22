"use client";

// 流水线阶段进度条：横向展示各阶段，按状态着色。

import type { StageStatus } from "@/lib/chat";
import { STAGE_MAP } from "@/lib/constants";

export default function PipelineStepper({ stages }: { stages: StageStatus[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-slate-800 bg-slate-900/60 p-3">
      {stages.map((stage, idx) => {
        const meta = STAGE_MAP[stage.node];
        if (!meta) return null;
        const isRunning = stage.state === "running";
        const isDone = stage.state === "done";

        const stateClass = isDone
          ? "text-green-400 border-green-500/40 bg-green-500/10"
          : isRunning
            ? "text-amber-300 border-amber-400/50 bg-amber-400/10 animate-pulse"
            : "text-slate-500 border-slate-700 bg-slate-800/40";

        return (
          <div key={stage.node} className="flex items-center gap-1.5">
            {idx > 0 && <span className="text-slate-700">›</span>}
            <span
              title={meta.label}
              className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-xs font-medium transition-colors ${stateClass}`}
            >
              <span className="text-sm leading-none">{meta.icon}</span>
              <span>{meta.label}</span>
              {isDone && <span className="text-green-500">✓</span>}
            </span>
          </div>
        );
      })}
    </div>
  );
}
