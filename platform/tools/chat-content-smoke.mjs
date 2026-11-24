import assert from "node:assert/strict";
import { filterDisplayMessages, isPlaceholderAssistant } from "../web/src/utils/chatContent.ts";

for (const text of ["", "  ", ".", "...", "…", "null", "undefined", "No content", "（无内容）", "assistant", "N/A"]) {
  assert.equal(isPlaceholderAssistant(text), true, JSON.stringify(text));
}
for (const text of ["OK", "好的", "无法完成请求", "修改完成", "已更新 E-report/main.tex"]) {
  assert.equal(isPlaceholderAssistant(text), false, text);
}
const messages = filterDisplayMessages([
  {role: "assistant", content: "..."},
  {role: "assistant", content: "", reasoning: "actual reasoning"},
  {role: "assistant", content: "", cancelled: true},
  {role: "assistant", content: "", tool_calls: [{name: "write"}]},
  {role: "assistant", content: "修改完成"},
  {role: "assistant", content: "OK"},
  {role: "user", content: "next"},
]);
assert.equal(messages.length, 6);
assert.equal(messages[0].reasoning, "actual reasoning");
assert.equal(messages[1].cancelled, true);
assert.equal(messages[2].tool_calls.length, 1);
assert.equal(messages[3].content, "修改完成");
assert.equal(messages[4].content, "OK");
assert.equal(messages[5].content, "next");
console.log("Empty/placeholder filtering vs reasoning/cancel/tool/modification summaries: OK");
