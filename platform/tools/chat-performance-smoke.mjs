import assert from "node:assert/strict";
import {
  CHAT_LIMITS,
  chatRenderCacheInfo,
  limitMarkdownSource,
  renderChatBlockHtml,
  resetChatRenderCache,
} from "../web/src/utils/chatRender.ts";

const limited = limitMarkdownSource("x".repeat(CHAT_LIMITS.responseBytes + 100));
assert.equal(limited.truncated, true);
assert.equal(limited.text.length, CHAT_LIMITS.responseBytes);
assert.equal(limitMarkdownSource("short").truncated, false);
const oversized = "BEGIN_MARK" + "x".repeat(600000) + "END_MARK";
const oversizedTail = limitMarkdownSource(oversized);
assert.equal(oversized.length, 600018);
assert.equal(oversizedTail.text.length, CHAT_LIMITS.responseBytes);
assert.ok(oversizedTail.truncated && !oversizedTail.text.includes("BEGIN_MARK") && oversizedTail.text.endsWith("END_MARK"));
const boundary = "中".repeat((CHAT_LIMITS.responseBytes - 8) / 3) + "🧪END!";
assert.equal(Buffer.byteLength(boundary), CHAT_LIMITS.responseBytes);
assert.deepEqual(limitMarkdownSource(boundary), {text: boundary, truncated: false});
const multibyteTail = limitMarkdownSource("丢弃" + "🧪".repeat(CHAT_LIMITS.responseBytes / 4) + "尾");
assert.equal(multibyteTail.truncated, true);
assert.equal(Buffer.byteLength(multibyteTail.text), CHAT_LIMITS.responseBytes - 1);
assert.ok(multibyteTail.text.startsWith("🧪") && multibyteTail.text.endsWith("尾"));
const largeCode = `\`\`\`python\n${"print('x')\n".repeat(4000)}\n${"$x$"}\n\`\`\``;
assert.ok(largeCode.length > CHAT_LIMITS.codeHighlightBytes);
assert.deepEqual(renderChatBlockHtml(largeCode, () => { throw new Error("高亮不应执行"); }), {kind: "plain", text: largeCode});
const unicodeCode = `~~~text\n${"温度".repeat(6000)}\n~~~`;
assert.ok(unicodeCode.length < CHAT_LIMITS.codeHighlightBytes);
assert.deepEqual(renderChatBlockHtml(unicodeCode, () => assert.fail("Multibyte code exceeded the byte budget")),
  {kind: "plain", text: unicodeCode});
const small = renderChatBlockHtml("Hello **world**", () => "<strong>world</strong>");
assert.equal(small.kind, "html");
assert.ok(small.html.includes("<strong>world</strong>"));
resetChatRenderCache();
for (let i = 0; i < 80; i++) renderChatBlockHtml(`Paragraph ${i} **bold**`, source => `<p>${source}</p>`);
const info = chatRenderCacheInfo();
assert.ok(info.entries <= CHAT_LIMITS.cacheItems, info);
assert.ok(info.bytes <= CHAT_LIMITS.cacheBytes, info);
resetChatRenderCache();
const largeEntry = "温度".repeat(40000);
renderChatBlockHtml(`${largeEntry}1`, source => source);
renderChatBlockHtml(`${largeEntry}2`, source => source);
assert.deepEqual(chatRenderCacheInfo(), {entries: 1, bytes: 480002});
console.log("Chat response/code/cache limits and plain-code degradation: OK", info);
