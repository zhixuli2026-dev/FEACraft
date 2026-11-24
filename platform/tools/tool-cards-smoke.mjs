import assert from "node:assert/strict";
import { projectToolCards, upsertToolCard } from "../web/src/utils/toolCards.ts";

const events = [
  {type: "tool_state", payload: {tool_call_id: "call-1", tool: "read_text_file", status: "queued", args: {path: "a"}}},
  {type: "tool_state", payload: {tool_call_id: "call-1", tool: "read_text_file", status: "running", args: {path: "a"}}},
  {type: "tool_result", payload: {tool_call_id: "call-1", tool: "read_text_file", status: "completed", ok: true, result: "contents", args: {path: "a"}}},
];
const projected = projectToolCards(events, []);
assert.equal(projected.length, 1);
assert.equal(projected[0].status, "completed");
assert.equal(projected[0].result, "contents");

const interrupted = projectToolCards([], [{id: "42:call-2", tool: "run_shell_command", status: "interrupted", args: {}, result: ""}]);
assert.equal(interrupted.length, 1);
assert.equal(interrupted[0].id, "call-2");
assert.equal(interrupted[0].status, "interrupted");
const pending = projectToolCards([], [{id: "42:call-3", tool: "propose_file_update", status: "pending", args: {}, result: ""}]);
assert.equal(pending[0].status, "awaiting_approval");
const rejected = projectToolCards([], [{id: "42:call-4", tool: "propose_file_update", status: "rejected", args: {}, result: "no"}]);
assert.equal(rejected[0].status, "cancelled");
const legacy = projectToolCards([{type: "tool_result", payload: {tool: "执行命令", ok: false, result: "old"}}], []);
assert.equal(legacy.length, 1);
assert.equal(legacy[0].status, "error");

const running = upsertToolCard([], {id: "call-5", tool: "list_files", status: "running", ok: false, result: ""});
const done = upsertToolCard(running, {id: "call-5", tool: "list_files", status: "completed", ok: true, result: "files"});
assert.equal(done.length, 1);
assert.equal(done[0].status, "completed");
console.log("Tool queued/running/final merge and interrupted recovery projection: OK");
