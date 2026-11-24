import assert from "node:assert/strict";
globalThis.localStorage = { getItem: () => null };
const { acceptCadSceneNotice } = await import("../web/src/utils/cadSceneFeed.ts");

const current = {type: "scene.replace", case_id: 8, version: 14};
const reset = {type: "scene.snapshot", case_id: 8, version: 14, source: "case"};
assert.equal(acceptCadSceneNotice(current, 8, null, true), true);
assert.equal(acceptCadSceneNotice(current, 8, 13, false), true);
assert.equal(acceptCadSceneNotice(current, 8, 14, false), false);
assert.equal(acceptCadSceneNotice({...current, type: "scene.clear", version: 12}, 8, 14, true), false);
assert.equal(acceptCadSceneNotice(reset, 8, 14, false), false);
assert.equal(acceptCadSceneNotice(reset, 8, 14, true), true);
assert.equal(acceptCadSceneNotice({...reset, version: 12}, 8, 14, true), false);
assert.equal(acceptCadSceneNotice({...reset, source: "unknown"}, 8, 12, true), false);
assert.equal(acceptCadSceneNotice({...current, case_id: 9}, 8, 13, false), false);
assert.equal(acceptCadSceneNotice({...current, version: Number.MAX_SAFE_INTEGER + 1}, 8, 13, false), false);
assert.equal(acceptCadSceneNotice({...current, version: undefined}, 8, null, false), true);
assert.equal(acceptCadSceneNotice({...current, version: undefined}, 8, 14, false), false);
console.log("CAD 场景版本、重连快照、错误顺序及旧服务器通知断言通过");
