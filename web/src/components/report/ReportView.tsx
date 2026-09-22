"use client";

// 最终报告渲染：把 final_report 的各个 section 渲染为 Markdown。

import type { FinalReport, Issue } from "@/lib/types";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface SectionDef {
  key: string;
  icon: string;
  title: string;
  color: string;
}

const SECTIONS: SectionDef[] = [
  { key: "executive_summary", icon: "📌", title: "核心观点摘要", color: "text-cyan-300" },
  { key: "investment_viewpoint", icon: "🎯", title: "投资观点展开", color: "text-fuchsia-300" },
  { key: "scenario_analysis", icon: "🔮", title: "情景分析", color: "text-fuchsia-300" },
  { key: "key_metrics", icon: "📈", title: "核心指标", color: "text-cyan-300" },
  { key: "signal_analysis", icon: "🚨", title: "风险信号", color: "text-cyan-300" },
  { key: "anomaly_detection", icon: "🔍", title: "勾稽关系异常", color: "text-cyan-300" },
  { key: "conflict_analysis", icon: "🔬", title: "数据矛盾", color: "text-fuchsia-300" },
  { key: "dimension_analysis", icon: "🏛", title: "维度分析", color: "text-cyan-300" },
  { key: "review_findings", icon: "🔎", title: "审查发现", color: "text-cyan-300" },
  { key: "falsification_conditions", icon: "⚖️", title: "可证伪条件", color: "text-yellow-300" },
  { key: "risks", icon: "⚠️", title: "主要风险", color: "text-red-300" },
  { key: "disclaimer", icon: "", title: "免责声明", color: "text-slate-500" },
];

// 与 CLI renderer 中"未生成/未检测"的占位文案保持一致，避免渲染空段。
const SKIP_VALUES = new Set([
  "未生成独立投资观点，以下为结构化数据分析。",
  "未生成情景分析。",
  "未检测到显著数据矛盾，多维度指标之间逻辑自洽。",
  "未执行审查",
  "无可证伪条件。",
]);

function ConfidenceBadge({ level }: { level?: string }) {
  if (!level || level === "unknown") return null;
  const color =
    level === "high"
      ? "bg-green-500/15 text-green-400"
      : level === "medium"
        ? "bg-yellow-500/15 text-yellow-300"
        : "bg-red-500/15 text-red-400";
  return (
    <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${color}`}>
      置信度 {level}
    </span>
  );
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-sm prose-invert max-w-none text-slate-300">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

export default function ReportView({
  report,
  issues,
}: {
  report: FinalReport | null;
  issues?: Issue[];
}) {
  if (!report) return null;

  const sections = report.sections ?? {};
  const riskCount = report.risk_count ?? {};

  return (
    <div className="mt-3 space-y-4 rounded-xl border border-slate-800 bg-slate-900/50 p-4">
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-800 pb-3">
        <h2 className="text-lg font-bold text-green-300">📋 {report.title ?? "投资分析报告"}</h2>
        <ConfidenceBadge level={report.overall_confidence} />
        {Object.keys(riskCount).length > 0 && (
          <div className="flex gap-2 text-xs text-slate-400">
            {riskCount.high ? <span className="text-red-400">高风险 {riskCount.high}</span> : null}
            {riskCount.medium ? (
              <span className="text-yellow-300">中风险 {riskCount.medium}</span>
            ) : null}
            {riskCount.low ? <span>低风险 {riskCount.low}</span> : null}
            {riskCount.blocked ? <span>阻塞 {riskCount.blocked}</span> : null}
          </div>
        )}
      </div>

      {report.summary && (
        <div>
          <div className="mb-1 text-sm font-semibold text-white">💡 摘要</div>
          <p className="text-sm text-slate-300">{report.summary}</p>
        </div>
      )}

      {SECTIONS.map((sec) => {
        const value = sections[sec.key];
        if (!value || SKIP_VALUES.has(value)) return null;
        return (
          <div key={sec.key}>
            <div className={`mb-1 text-sm font-semibold ${sec.color}`}>
              {sec.icon && <span className="mr-1">{sec.icon}</span>}
              {sec.title}
            </div>
            <Markdown text={value} />
          </div>
        );
      })}

      {issues && issues.length > 0 && (
        <div className="border-t border-slate-800 pt-3">
          <div className="mb-1 text-sm font-semibold text-yellow-300">🐞 系统问题（{issues.length}）</div>
          <ul className="space-y-1">
            {issues.map((issue, i) => (
              <li key={i} className="text-xs text-slate-400">
                <span className="text-red-400">[{issue.severity}]</span> {issue.category}:{" "}
                {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
