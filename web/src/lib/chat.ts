// 聊天消息与流水线运行时状态的类型定义（前端内部使用）。

import type { FinalReport, Issue } from "./types";

export type Role = "user" | "assistant";

export interface ToolCallRecord {
  step: number;
  agent: string;
  tool: string;
  kind: "tool" | "subagent";
  args: Record<string, unknown>;
  status?: string;
}

export interface ThinkingRecord {
  step: number;
  agent: string;
  text: string;
}

export interface StageStatus {
  node: string;
  state: "pending" | "running" | "done";
  elapsed?: number;
}

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  // 助手消息可能附带的结构化数据
  report?: FinalReport | null;
  issues?: Issue[];
  thinking?: ThinkingRecord[];
  toolCalls?: ToolCallRecord[];
  finalAnswer?: string;
}
