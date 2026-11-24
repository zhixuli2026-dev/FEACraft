import assert from "node:assert/strict";
import { toolPresentation, toolStatusLabel } from "../web/src/utils/toolPresentation.ts";

assert.deepEqual(toolPresentation("read_file", "queued"), {label: "读取文件", kind: "file"});
assert.deepEqual(toolPresentation("run_windows_command", "completed"), {label: "已运行 PowerShell", kind: "terminal"});
assert.deepEqual(toolPresentation("run_wsl_command", "running"), {label: "运行 WSL 命令", kind: "terminal"});
assert.deepEqual(toolPresentation("update_plan", "awaiting_approval"), {label: "更新计划", kind: "plan"});
assert.deepEqual(toolPresentation("执行命令", "error"), {label: "执行命令", kind: "terminal"});
assert.deepEqual(toolPresentation("run_shell_command", "completed"), {label: "run_shell_command", kind: "terminal"});
assert.deepEqual(toolPresentation("web_fetch", "completed"), {label: "已读取网页", kind: "globe"});
assert.equal(toolStatusLabel("queued"), "等待运行");
assert.equal(toolStatusLabel("running"), "正在运行");
assert.equal(toolStatusLabel("awaiting_approval"), "等待确认");
assert.equal(toolStatusLabel("completed"), "已完成");
assert.equal(toolStatusLabel("error"), "失败");
assert.equal(toolStatusLabel("cancelled"), "已取消");
assert.equal(toolStatusLabel("interrupted"), "已中断");
assert.equal(toolStatusLabel("unknown-state"), "状态未知");
console.log("Tool status labels/type icons/legacy titles: OK");
