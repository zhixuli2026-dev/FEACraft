import { renderChatMd } from "./chatMd.ts";

export const CHAT_LIMITS = {
  responseBytes: 512 * 1024,
  codeHighlightBytes: 32 * 1024,
  cacheItems: 64,
  cacheBytes: 512 * 1024,
} as const;

const encoder = new TextEncoder();
const cache = new Map<string, { html: string; bytes: number }>();
let cacheBytes = 0;

export function limitMarkdownSource(source: string): { text: string; truncated: boolean } {
  if (source.length * 3 <= CHAT_LIMITS.responseBytes) return { text: source, truncated: false };
  let remaining = CHAT_LIMITS.responseBytes;
  let start = source.length;
  while (start > 0) {
    const last = source.charCodeAt(start - 1);
    const previous = source.charCodeAt(start - 2);
    const pair = last >= 0xdc00 && last <= 0xdfff && previous >= 0xd800 && previous <= 0xdbff;
    const size = pair ? 4 : last < 0x80 ? 1 : last < 0x800 ? 2 : 3;
    if (size > remaining) break;
    remaining -= size;
    start -= pair ? 2 : 1;
  }
  // 仅限制渲染，数据库和复制仍使用完整消息。
  return { text: source.slice(start), truncated: start > 0 };
}

export function chatRenderCacheInfo() {
  return { entries: cache.size, bytes: cacheBytes };
}

export function resetChatRenderCache() {
  cache.clear();
  cacheBytes = 0;
}

function insertCache(key: string, html: string) {
  if (key.length + html.length > CHAT_LIMITS.cacheBytes) return;
  const itemBytes = encoder.encode(key).length + encoder.encode(html).length;
  if (itemBytes > CHAT_LIMITS.cacheBytes) return;
  while (cache.size >= CHAT_LIMITS.cacheItems || cacheBytes + itemBytes > CHAT_LIMITS.cacheBytes) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    const previous = cache.get(oldest)!;
    cache.delete(oldest);
    cacheBytes -= previous.bytes;
  }
  cache.set(key, { html, bytes: itemBytes });
  cacheBytes += itemBytes;
}

export function renderChatBlockHtml(
  text: string,
  renderer: (source: string) => string = renderChatMd,
): { kind: "html"; html: string } | { kind: "plain"; text: string } {
  const trimmed = text.trimStart();
  if ((trimmed.startsWith("```") || trimmed.startsWith("~~~")) &&
      text.length * 3 > CHAT_LIMITS.codeHighlightBytes && encoder.encode(text).length > CHAT_LIMITS.codeHighlightBytes) {
    return { kind: "plain", text };
  }
  const cached = cache.get(text);
  if (cached !== undefined) {
    cache.delete(text);
    cache.set(text, cached);
    return { kind: "html", html: cached.html };
  }
  const html = renderer(text);
  insertCache(text, html);
  return { kind: "html", html };
}
