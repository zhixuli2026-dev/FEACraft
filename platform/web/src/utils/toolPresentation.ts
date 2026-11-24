export type ToolKind = "reasoning" | "folder" | "file" | "search" | "globe" |
  "terminal" | "edit" | "plan" | "image" | "tool";

const TABLE: Record<string, [string, string, ToolKind]> = {
  read_text_file: ["读取文件", "已读取文件", "file"], read_file: ["读取文件", "已读取文件", "file"],
  list_files: ["查看目录", "已查看目录", "folder"], search_text: ["搜索文本", "已搜索文本", "search"],
  web_search: ["搜索网页", "已搜索网页", "globe"], web_fetch: ["读取网页", "已读取网页", "globe"],
  wsl_command: ["运行 WSL 命令", "已运行 WSL 命令", "terminal"],
  windows_powershell: ["运行 PowerShell", "已运行 PowerShell", "terminal"],
  run_wsl_command: ["运行 WSL 命令", "已运行 WSL 命令", "terminal"],
  run_windows_command: ["运行 PowerShell", "已运行 PowerShell", "terminal"],
  write_text_file: ["写入文件", "已写入文件", "edit"], write_file: ["写入文件", "已写入文件", "edit"],
  edit_file: ["编辑文件", "已编辑文件", "edit"], apply_patch: ["应用修改", "已应用修改", "edit"],
  file_update_proposal: ["生成文件修改", "已生成文件修改", "edit"],
  text_replacement_proposal: ["生成文本替换", "已生成文本替换", "edit"],
  propose_file_update: ["生成文件修改", "已生成文件修改", "edit"],
  propose_text_replacement: ["生成文本替换", "已生成文本替换", "edit"],
  update_plan: ["更新计划", "已更新计划", "plan"], update_goal: ["更新目标", "已更新目标", "plan"],
  view_image: ["查看图像", "已查看图像", "image"], thinking: ["正在思考", "思考过程", "reasoning"],
  thought: ["正在思考", "思考过程", "reasoning"], preparing_tool: ["准备工具", "已准备工具", "tool"],
};

const LEGACY: Record<string, ToolKind> = {
  "思考": "reasoning", "执行命令": "terminal", "文件修改": "edit", "本轮修改差异": "edit",
};

export function toolPresentation(title: string, status = "completed", semanticKind?: ToolKind) {
  const raw = title || "Tool";
  if (semanticKind) return {label: raw, kind: semanticKind};
  if (LEGACY[raw]) return {label: raw, kind: LEGACY[raw]};
  const key = raw.toLowerCase().replace(/[\s-]+/g, "_");
  const item = TABLE[key];
  if (item) return {label: status.toLowerCase() === "completed" ? item[1] : item[0], kind: item[2]};
  const kind: ToolKind = /(^|_)list|director/.test(key) ? "folder" :
    /read.*file|(^|_)file/.test(key) ? "file" : /search|find/.test(key) ? "search" :
    /web|http|url/.test(key) ? "globe" : /command|shell|terminal|powershell|wsl|exec/.test(key) ? "terminal" :
    /write|edit|patch|replace/.test(key) ? "edit" : /plan|todo|goal/.test(key) ? "plan" :
    /image|screenshot/.test(key) ? "image" : "tool";
  return {label: raw, kind};
}

export function toolStatusLabel(status = "completed") {
  const labels: Record<string, string> = {
    running: "正在运行", streaming: "正在运行", queued: "等待运行",
    awaiting_approval: "等待确认", completed: "已完成", error: "失败",
    cancelled: "已取消", interrupted: "已中断",
  };
  return labels[status.toLowerCase()] ?? "状态未知";
}

export const TOOL_KIND_ICONS: Record<ToolKind, string> = {
  reasoning: "🧠", folder: "📁", file: "📄", search: "🔍", globe: "🌐",
  terminal: "⌨️", edit: "✏️", plan: "☑️", image: "🖼️", tool: "🔧",
};
