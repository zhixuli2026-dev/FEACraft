import assert from "node:assert/strict";
import { createHash } from "node:crypto";

globalThis.localStorage = {getItem:() => null, setItem:() => {}, removeItem:() => {}};
const { useStore } = await import("../web/src/store.ts");
const { api, ApiError } = await import("../web/src/api.ts");
const hash = text => createHash("sha256").update(text).digest("hex");
const cases = new Map();
let calls = [];
let gate = null;
let failTree = false;
function hold(method) {
  let release, reached;
  const ready = new Promise(resolve => reached = resolve);
  const wait = new Promise(resolve => release = resolve);
  gate = {method, wait, reached};
  return {ready, release};
}
function reset() {
  cases.set(1, {content:"original 中文", revision:4});
  cases.set(2, {content:"other case", revision:0});
  calls = []; gate = null; failTree = false;
  const list = [{id:1, name:"one"}, {id:2, name:"two"}];
  useStore.setState({user:{id:1, email:"editor@example.test", name:"Editor"},
    currentWs:{id:1, name:"Fixture workspace"}, currentCase:list[0], cases:list,
    openFiles:{}, activeFile:null, fileTree:[]});
}
const response = (body, status=200) => new Response(JSON.stringify(body), {status});
globalThis.fetch = async (url, options={}) => {
  const method = options.method ?? "GET";
  const body = options.body ? JSON.parse(options.body) : undefined;
  calls.push({url,method,body});
  const held = gate?.method === method ? gate : null;
  if (held) { gate=null; held.reached(); await held.wait; }
  const match = url.match(/\/cases\/(\d+)(.*)/);
  const state = cases.get(Number(match?.[1]));
  assert.ok(state, url);
  const suffix = match[2];
  if (!suffix) return response({id:Number(match[1]), dict_revision:state.revision, workflow_state:"A"});
  if (suffix === "/tree") return response(failTree ? {detail:"refresh offline"} : {entries:[]}, failTree ? 500 : 200);
  if (method === "GET") return response({path:"D-post/input.txt", kind:"text", content:state.content,
    content_sha256:hash(state.content), dict_revision:state.revision, size:state.content.length});
  assert.equal(method, "PUT");
  if (body.expected_sha256 !== hash(state.content) ||
      (suffix === "/caseDict" && body.expected_revision !== state.revision))
    return response({detail:{code:"file_conflict",message:"文件内容已变化",path:"test"}}, 409);
  state.content = body.content;
  if (suffix === "/caseDict") state.revision++;
  return response({ok:true, content_sha256:hash(state.content), dict_revision:state.revision});
};

const path = "0-caseDict/caseDict";
const checks = [];
reset();
await assert.rejects(api.put("/cases/1/caseDict", {content:"stale"}), error =>
  error instanceof ApiError && error.status === 409 && error.code === "file_conflict" && !error.message.includes("[object Object]"));
checks.push("structured_conflict_message_and_status_are_preserved");

reset();
assert.equal(await useStore.getState().openFile(path), true);
useStore.getState().editContent(path, "submitted 中文🙂");
const first = hold("PUT");
const saving = useStore.getState().saveFile(path);
await first.ready;
assert.equal(await useStore.getState().saveFile(path), null);
useStore.getState().editContent(path, "typed while saving");
first.release(); await saving;
let file = useStore.getState().openFiles[path];
assert.equal(file.content, "typed while saving");
assert.equal(file.dirty, true);
assert.equal(file.content_sha256, hash("submitted 中文🙂"));
assert.equal(file.dict_revision, 5);
assert.equal(calls.filter(call=>call.method==="PUT").length, 1);
assert.equal(calls.find(call=>call.method==="PUT").body.expected_revision, 4);
await useStore.getState().saveFile(path);
assert.equal(cases.get(1).content, "typed while saving");
assert.equal(useStore.getState().openFiles[path].dirty, false);
checks.push("one_inflight_save_preserves_later_keystrokes_and_advances_only_its_baseline");

useStore.getState().editContent(path, "committed before refresh");
const refreshing = hold("GET");
const refreshingSave = useStore.getState().saveFile(path);
await refreshing.ready;
useStore.getState().editContent(path, "new text during refresh");
assert.equal(useStore.getState().openFiles[path].saving, true);
assert.equal(await useStore.getState().saveFile(path), null);
refreshing.release(); await refreshingSave;
assert.equal(useStore.getState().openFiles[path].saving, false);
assert.equal(useStore.getState().openFiles[path].dirty, true);
await useStore.getState().saveFile(path);
assert.equal(cases.get(1).content, "new text during refresh");
checks.push("save_remains_inflight_until_refresh_finishes_without_clearing_new_input");

useStore.getState().editContent(path, "local draft");
cases.get(1).content = "external content";
await assert.rejects(useStore.getState().saveFile(path), ApiError);
file = useStore.getState().openFiles[path];
assert.equal(file.content, "local draft"); assert.equal(file.dirty, true);
assert.equal(file.conflict, true); assert.match(file.saveError, /草稿已保留/);
await useStore.getState().inspectServerVersion(path);
assert.equal(useStore.getState().openFiles[path].content_sha256, file.content_sha256);
useStore.getState().resolveFileConflict(path, true);
assert.equal(useStore.getState().openFiles[path].content, "local draft");
cases.get(1).content = "changed again after review";
await assert.rejects(useStore.getState().saveFile(path), ApiError);
await useStore.getState().inspectServerVersion(path);
useStore.getState().resolveFileConflict(path, false);
assert.equal(useStore.getState().openFiles[path].content, "changed again after review");
assert.equal(useStore.getState().openFiles[path].dirty, false);
checks.push("conflict_keeps_draft_review_does_not_approve_and_another_change_is_rejected");

reset();
const reading = hold("GET");
const open = useStore.getState().openFile(path);
await reading.ready;
useStore.setState({currentCase:{id:2,name:"two"}, openFiles:{}});
reading.release(); await open;
assert.deepEqual(useStore.getState().openFiles, {});
checks.push("late_open_response_does_not_inject_old_case_files");

reset(); await useStore.getState().openFile(path);
useStore.getState().editContent(path, "old case submitted");
const delayed = hold("PUT");
const oldSave = useStore.getState().saveFile(path);
await delayed.ready;
await useStore.getState().selectCase({id:2,name:"two"});
await useStore.getState().openFile(path);
useStore.getState().editContent(path, "new case draft");
delayed.release(); await oldSave;
assert.equal(useStore.getState().openFiles[path].content, "new case draft");
assert.equal(useStore.getState().openFiles[path].dirty, true);
checks.push("late_save_response_cannot_change_another_case_editor");

reset(); await useStore.getState().openFile(path);
useStore.getState().editContent(path, "old editor submitted");
const closing = hold("PUT");
const closedSave = useStore.getState().saveFile(path);
await closing.ready;
useStore.getState().closeFile(path);
await useStore.getState().openFile(path);
const editorId = useStore.getState().openFiles[path].editorId;
useStore.getState().editContent(path, "new editor draft");
closing.release(); await closedSave;
assert.equal(useStore.getState().openFiles[path].editorId, editorId);
assert.equal(useStore.getState().openFiles[path].content, "new editor draft");
assert.equal(useStore.getState().openFiles[path].dirty, true);
checks.push("closed_and_reopened_editor_is_not_updated_by_previous_save");

reset(); await useStore.getState().openFile("D-post/input.txt");
useStore.getState().editContent("D-post/input.txt", "ordinary save");
failTree = true;
assert.ok((await useStore.getState().saveFile("D-post/input.txt")).ok);
assert.equal(useStore.getState().openFiles["D-post/input.txt"].dirty, false);
assert.equal("expected_revision" in calls.find(call=>call.method==="PUT").body, false);
checks.push("ordinary_file_uses_content_hash_and_refresh_error_does_not_undo_success");

reset(); await useStore.getState().openFile(path);
useStore.getState().editContent(path, "save all text");
const allGate = hold("PUT");
const saveAll = useStore.getState().saveAll();
await allGate.ready;
useStore.getState().editContent(path, "new draft during save all");
allGate.release();
await assert.rejects(saveAll, /仍有编辑尚未保存/);
assert.equal(useStore.getState().openFiles[path].dirty, true);
checks.push("save_all_does_not_claim_newer_drafts_were_saved");

reset(); await useStore.getState().openFile(path);
useStore.setState({openFiles:{[path]:{...useStore.getState().openFiles[path], content_sha256:undefined, dirty:true}}});
await assert.rejects(useStore.getState().saveFile(path), /无法确认文件版本/);
assert.equal(calls.some(call=>call.method==="PUT"), false);
checks.push("missing_baseline_never_sends_an_unconditional_write");

reset(); await useStore.getState().openFile(path);
useStore.getState().editContent(path, "invalid stage change");
const originalFetch = globalThis.fetch;
try {
  globalThis.fetch = async () => response({detail:"普通保存不得修改 workflowState；请确认阶段推进"}, 409);
  await assert.rejects(useStore.getState().saveFile(path), ApiError);
  assert.match(useStore.getState().openFiles[path].saveError, /不得修改 workflowState/);
  assert.equal(useStore.getState().openFiles[path].conflict, false);
  assert.equal(useStore.getState().openFiles[path].dirty, true);
} finally { globalThis.fetch = originalFetch; }
checks.push("stage_gate_409_is_not_misreported_as_an_external_edit_conflict");
console.log("EDITOR_WRITE_OK "+JSON.stringify({passed:true,count:checks.length,checks}));
