import assert from "node:assert/strict";
import { estimateContextUsage } from "../web/src/utils/contextUsage.ts";

const usage = estimateContextUsage(
  [{role: "user", content: "abcd"}, {role: "assistant", content: "efgh", reasoning: "R".repeat(100)}],
  "xy", "z", [{result: "12"}],
);
assert.deepEqual(usage, {chars: 13, estimatedTokens: 4});
assert.deepEqual(estimateContextUsage([]), {chars: 0, estimatedTokens: 0});
console.log("Context character/4 estimate excludes reasoning: OK");
