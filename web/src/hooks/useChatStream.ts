"use client";

// 聊天流式核心 hook：发起 SSE 请求，消费事件流，更新消息列表与流水线状态。

import type { ChatMessage, StageStatus, ThinkingRecord, ToolCallRecord } from "@/lib/chat";
import { STAGE_ORDER } from "@/lib/constants";
import { streamChat } from "@/lib/sse";
import type { FinalReport, Issue, StreamEvent } from "@/lib/types";
import { useCallback, useRef, useState } from "react";

export interface ChatFlags {
  enhance: boolean;
  llm_review: boolean;
  midterm: boolean;
}

export interface UseChatStreamReturn {
  messages: ChatMessage[];
  stages: StageStatus[];
  isStreaming: boolean;
  sessionId: string | null;
  flags: ChatFlags;
  setFlags: (flags: ChatFlags) => void;
  send: (query: string) => Promise<void>;
  clear: () => void;
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function initialStages(): StageStatus[] {
  return STAGE_ORDER.map((node) => ({
    node,
    state: "pending" as const,
  }));
}

export function useChatStream(): UseChatStreamReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stages, setStages] = useState<StageStatus[]>(initialStages());
  const [isStreaming, setIsStreaming] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [flags, setFlags] = useState<ChatFlags>({
    enhance: false,
    llm_review: false,
    midterm: false,
  });

  const abortRef = useRef<AbortController | null>(null);

  const send = useCallback(
    async (query: string) => {
      const trimmed = query.trim();
      if (!trimmed || isStreaming) return;

      // 用户消息入列
      const userMsg: ChatMessage = { id: makeId(), role: "user", content: trimmed };
      // 助手占位消息（流式填充）
      const assistantMsg: ChatMessage = { id: makeId(), role: "assistant", content: "" };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setStages(initialStages());
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      // 用于累积当前 assistant 消息的结构化字段
      let thinking: ThinkingRecord[] = [];
      let toolCalls: ToolCallRecord[] = [];
      let finalAnswer = "";
      let report: FinalReport | null = null;
      let issues: Issue[] = [];

      const updateAssistant = (patch: Partial<ChatMessage>) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMsg.id ? { ...m, ...patch } : m)),
        );
      };

      const handleEvent = (event: StreamEvent) => {
        switch (event.type) {
          case "stage_start":
          case "stage_done": {
            const state = event.type === "stage_start" ? "running" : "done";
            setStages((prev) =>
              prev.map((s) =>
                s.node === event.node
                  ? { ...s, state: state as StageStatus["state"], elapsed: event.elapsed }
                  : s,
              ),
            );
            break;
          }
          case "thinking": {
            thinking = [...thinking, { step: event.step, agent: event.agent, text: event.text }];
            updateAssistant({ thinking, content: event.text });
            break;
          }
          case "tool_call": {
            const rec: ToolCallRecord = {
              step: event.step,
              agent: event.agent,
              tool: event.tool,
              kind: event.kind,
              args: event.args,
            };
            toolCalls = [...toolCalls, rec];
            updateAssistant({ toolCalls });
            break;
          }
          case "tool_result": {
            // 匹配最近的同名 tool_call，标记完成
            for (let i = toolCalls.length - 1; i >= 0; i--) {
              if (toolCalls[i].tool === event.tool && toolCalls[i].status === undefined) {
                toolCalls = toolCalls.map((t, idx) =>
                  idx === i ? { ...t, status: event.status } : t,
                );
                break;
              }
            }
            updateAssistant({ toolCalls });
            break;
          }
          case "report": {
            report = event.payload.final_report ?? null;
            issues = event.payload.issues ?? [];
            updateAssistant({ report, issues });
            break;
          }
          case "done": {
            finalAnswer = event.final_answer;
            setSessionId((current) => current ?? makeId());
            break;
          }
          case "error": {
            updateAssistant({ content: `⚠️ ${event.message}` });
            break;
          }
        }
      };

      try {
        await streamChat(
          { query: trimmed, session_id: sessionId, ...flags },
          handleEvent,
          controller.signal,
        );
      } catch (err) {
        const message =
          err instanceof DOMException && err.name === "AbortError"
            ? "已中断"
            : `请求出错: ${err instanceof Error ? err.message : String(err)}`;
        updateAssistant({ content: `⚠️ ${message}` });
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [flags, isStreaming, sessionId],
  );

  const clear = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setStages(initialStages());
    setSessionId(null);
  }, []);

  return { messages, stages, isStreaming, sessionId, flags, setFlags, send, clear };
}
