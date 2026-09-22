// 流水线阶段元数据（移植自 alphabee/apps/cli/renderer.py 的 STAGE_MAP）。
// 后端按 node 名发 stage_start/stage_done 事件，前端据此渲染进度。

export interface StageMeta {
  icon: string;
  label: string;
  color: string; // tailwind 文本色类
}

export const STAGE_MAP: Record<string, StageMeta> = {
  collect_raw_facts: { icon: "📊", label: "事实采集", color: "text-cyan-400" },
  resolve_industry_context: { icon: "🏭", label: "行业语境解析", color: "text-cyan-400" },
  resolve_company_track: { icon: "🏷", label: "公司赛道解析", color: "text-cyan-400" },
  resolve_driver_profile: { icon: "🧭", label: "驱动画像解析", color: "text-cyan-400" },
  run_analysis_engines: { icon: "⚙️", label: "规则引擎计算", color: "text-cyan-400" },
  explore_conflicts: { icon: "🔬", label: "冲突探索", color: "text-fuchsia-400" },
  verify_hypotheses: { icon: "🧪", label: "假设验证", color: "text-fuchsia-400" },
  synthesize_insights: { icon: "💡", label: "观点综合", color: "text-blue-400" },
  run_thesis: { icon: "🏛", label: "投资论点生成", color: "text-blue-400" },
  review_thesis: { icon: "🔍", label: "论点审查", color: "text-fuchsia-400" },
  resolve_midterm_decision: { icon: "🎯", label: "中期决策", color: "text-green-400" },
  midterm_decision_reporter: { icon: "🗒️", label: "中期决策总结", color: "text-green-400" },
  generate_report: { icon: "📝", label: "报告生成", color: "text-blue-400" },
  review_report: { icon: "🛡️", label: "报告质量门控", color: "text-fuchsia-400" },
  finalize_message: { icon: "✅", label: "完成", color: "text-green-400" },
};

export const STAGE_ORDER: string[] = Object.keys(STAGE_MAP);
