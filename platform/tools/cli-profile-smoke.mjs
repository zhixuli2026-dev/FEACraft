import assert from "node:assert/strict";
import { commandForCliAgent } from "../web/src/utils/cliProfiles.ts";

const env = {PYTHONUTF8:"1", PYTHONIOENCODING:"utf-8", TERM:"xterm-256color"};
const command = commandForCliAgent("codex", "codex-wsl", env);
assert.equal(command, "env PYTHONUTF8='1' PYTHONIOENCODING='utf-8' TERM='xterm-256color' codex");
assert.equal(commandForCliAgent("codex", "default", env), "codex");
assert.equal(commandForCliAgent("opencode", "codex-wsl", env), "opencode");
assert.equal(commandForCliAgent("codex", "codex-wsl", {}), "codex");
assert.equal(commandForCliAgent("codex", "codex-wsl", {TERM: "xterm's $label"}), "env TERM='xterm'\"'\"'s $label' codex");
assert.throws(() => commandForCliAgent("codex", "codex-wsl", {"INVALID NAME": "value"}), /变量名称/);
console.log("codex-wsl compatibility command: OK");
