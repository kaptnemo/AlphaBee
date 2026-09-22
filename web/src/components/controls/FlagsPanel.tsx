"use client";

// 增强层 / LLM 审查 / 中期决策 开关。

import type { ChatFlags } from "@/hooks/useChatStream";

const FLAG_DEFS: { key: keyof ChatFlags; label: string; desc: string }[] = [
  { key: "enhance", label: "增强层", desc: "启用 LLM 增强（跨信号模式 + 行业语境化）" },
  { key: "llm_review", label: "LLM 审查", desc: "启用 LLM 审查层（定性证据审查）" },
  { key: "midterm", label: "中期决策", desc: "启用中期决策节点" },
];

export default function FlagsPanel({
  flags,
  onChange,
}: {
  flags: ChatFlags;
  onChange: (flags: ChatFlags) => void;
}) {
  const toggle = (key: keyof ChatFlags) => {
    onChange({ ...flags, [key]: !flags[key] });
  };

  return (
    <div className="flex items-center gap-2">
      {FLAG_DEFS.map((def) => {
        const active = flags[def.key];
        return (
          <button
            key={def.key}
            type="button"
            title={def.desc}
            onClick={() => toggle(def.key)}
            className={`rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors ${
              active
                ? "border-blue-500/60 bg-blue-500/15 text-blue-300"
                : "border-slate-700 bg-slate-900 text-slate-500 hover:text-slate-300"
            }`}
          >
            {def.label} {active ? "●" : "○"}
          </button>
        );
      })}
    </div>
  );
}
