// 前后端共享事件契约的 TS 镜像（与 alphabee/apps/web/events.py 对齐）。
// 修改事件字段时，两端必须同步。

export type EventType =
  | "stage_start"
  | "stage_done"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "report"
  | "done"
  | "error";

export interface StageEvent {
  type: "stage_start" | "stage_done";
  node: string;
  label: string;
  icon: string;
  elapsed: number;
}

export interface ThinkingEvent {
  type: "thinking";
  step: number;
  agent: string;
  text: string;
}

export interface ToolCallEvent {
  type: "tool_call";
  step: number;
  agent: string;
  tool: string;
  kind: "tool" | "subagent";
  args: Record<string, unknown>;
}

export interface ToolResultEvent {
  type: "tool_result";
  step: number;
  agent: string;
  tool: string;
  status: string;
  length: number;
}

export interface ReportEvent {
  type: "report";
  payload: ReportPayload;
}

export interface DoneEvent {
  type: "done";
  final_answer: string;
  total_steps: number;
  total_time: number;
  flags: {
    enhance: boolean;
    llm_review: boolean;
    midterm: boolean;
  };
}

export interface ErrorEvent {
  type: "error";
  message: string;
  traceback: string | null;
}

export type StreamEvent =
  | StageEvent
  | ThinkingEvent
  | ToolCallEvent
  | ToolResultEvent
  | ReportEvent
  | DoneEvent
  | ErrorEvent;

// ── 最终报告负载（alphabee/orchestrator/agent.py finalize_message 产出）──

export interface ReportPayload {
  run: unknown;
  final_report: FinalReport | null;
  midterm_decision: Record<string, unknown> | null;
  artifacts: unknown[];
  decisions: unknown[];
  issues: Issue[];
}

export interface FinalReport {
  title?: string;
  summary?: string;
  overall_confidence?: string;
  risk_count?: Record<string, number>;
  sections?: Record<string, string>;
}

export interface Issue {
  severity?: string;
  category?: string;
  message?: string;
}

// ── 请求体 ──

export interface ChatRequest {
  query: string;
  session_id?: string | null;
  enhance?: boolean;
  llm_review?: boolean;
  midterm?: boolean;
}
