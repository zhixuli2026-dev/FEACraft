export interface ContextUsage {
  chars: number;
  estimatedTokens: number;
}

/** 粗略文本上下文估算（字符数/4），不是 provider 计费 token，也不把 reasoning 当模型历史。 */
export function estimateContextUsage(
  messages: { content: string }[],
  draft = "",
  streamText = "",
  toolResults: { result: string }[] = [],
): ContextUsage {
  const messageChars = messages.reduce((total, message) => total + message.content.length, 0);
  const toolChars = toolResults.reduce((total, result) => total + result.result.length, 0);
  const chars = messageChars + toolChars + draft.length + streamText.length;
  return { chars, estimatedTokens: Math.ceil(chars / 4) };
}
