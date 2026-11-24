import assert from "node:assert/strict";
import {formatRunDuration, projectRunTimings, runDurationMs} from "../web/src/utils/runTiming.ts";

const events = [
  {type: "run_state", payload: {run_id: "r1", status: "started", started_at: 100}},
  {type: "message", payload: {run_id: "r1", content: "ignored"}},
  {type: "run_state", payload: {run_id: "r1", status: "completed", started_at: 100, ended_at: 102.25, duration_ms: 2250}},
  {type: "run_state", payload: {run_id: "r2", status: "started", started_at: 200}},
];
const timings = projectRunTimings(events);
assert.deepEqual(timings.r1, {runId: "r1", status: "completed", startedAt: 100000, endedAt: 102250, durationMs: 2250});
assert.equal(runDurationMs(timings.r1, 999000), 2250);
assert.equal(runDurationMs(timings.r2, 203500), 3500);
assert.equal(formatRunDuration(500), "<1 秒");
assert.equal(formatRunDuration(2250), "2.3 秒");
assert.equal(formatRunDuration(12000), "12 秒");
assert.equal(formatRunDuration(125000), "2 分 5 秒");
assert.equal(formatRunDuration(3720000), "1 时 2 分");
console.log("Run timing projection, fixed start and duration formatting: OK");
