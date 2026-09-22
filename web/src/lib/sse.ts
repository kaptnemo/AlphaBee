// 基于 fetch ReadableStream 的 SSE 客户端。
// 不用 EventSource，因为 EventSource 只支持 GET、无法携带 POST JSON 请求体。

import type { StreamEvent } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8010";

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

/**
 * POST /api/chat，逐条解析 SSE 事件。
 * @param onEvent 每条事件回调
 * @param signal  用于取消请求
 */
export async function streamChat(
  body: {
    query: string;
    session_id?: string | null;
    enhance?: boolean;
    llm_review?: boolean;
    midterm?: boolean;
  },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(apiUrl("/api/chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    throw new Error(`请求失败: HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE 事件以空行分隔；逐行提取 "data: {...}"
    let newlineIndex: number;
    while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);

      if (!line.startsWith("data:")) continue;
      const json = line.slice(5).trim();
      if (!json) continue;

      try {
        const event = JSON.parse(json) as StreamEvent;
        onEvent(event);
      } catch {
        // 忽略无法解析的行（心跳/注释）
      }
    }
  }
}

export async function healthCheck(): Promise<boolean> {
  try {
    const res = await fetch(apiUrl("/api/health"));
    return res.ok;
  } catch {
    return false;
  }
}
