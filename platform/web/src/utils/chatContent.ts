const PLACEHOLDERS = new Set([
  "", ".", "..", "...", "…", "。", "-", "—", "null", "undefined", "n/a", "na",
  "no content", "no response", "empty", "（无内容）", "无内容", "assistant", "ai",
]);

export function isPlaceholderAssistant(content: string): boolean {
  return PLACEHOLDERS.has(content.trim().toLowerCase());
}

/** 显示层过滤空回复；DB 事件原样保留，reasoning/取消/工具调用和修改摘要不被误删。 */
export function filterDisplayMessages<T extends {
  role: string;
  content: string;
  reasoning?: string;
  cancelled?: boolean;
  tool_calls?: unknown[];
}>(messages: T[]): T[] {
  return messages.filter((message) => {
    if (message.role !== "assistant") return true;
    if (message.reasoning?.trim() || message.cancelled || message.tool_calls?.length) return true;
    return !isPlaceholderAssistant(message.content);
  });
}
