// dockview api 的全局持有者（Shell onReady 时设置），供树面板等外部触发标签切换
import type { DockviewApi } from "dockview";

let dockApi: DockviewApi | null = null;
let compactLayout = false;

export const workbenchPanels = [
  { id: "tree", title: "项目与文件" }, { id: "caseinfo", title: "算例与工作流" },
  { id: "jobs", title: "作业与日志" }, { id: "terminal", title: "终端" },
  { id: "agent", title: "Agent" }, { id: "editor", title: "编辑器" },
  { id: "geometry", title: "几何预览" }, { id: "viewer", title: "查看器" },
  { id: "flotherm", title: "FloTHERM" },
  { id: "viz", title: "可视化" }, { id: "texo", title: "公式识别" },
  { id: "formula", title: "公式调色板" }, { id: "scnet", title: "SCNET" },
  { id: "settings", title: "设置与账户" }, { id: "providers", title: "模型配置" },
] as const;

export function setDockApi(api: DockviewApi | null) {
  dockApi = api;
}

export function setCompactLayout(enabled: boolean) {
  compactLayout = enabled;
  if (!dockApi) return;
  if (enabled && dockApi.activePanel) dockApi.maximizeGroup(dockApi.activePanel);
  else if (!enabled && dockApi.hasMaximizedGroup()) dockApi.exitMaximizedGroup();
}

export function activatePanel(id: string) {
  if (!dockApi) return;
  let panel = dockApi.getPanel(id);
  const known = workbenchPanels.find(item => item.id === id);
  if (!panel && known) panel = dockApi.addPanel({ id, component: id, title: known.title,
    ...(id === "providers" && dockApi.getPanel("settings") ? { position: { referencePanel: "settings" } } : {}) });
  if (!panel) return;
  if (compactLayout && dockApi.hasMaximizedGroup() && dockApi.activePanel?.group !== panel.group)
    dockApi.exitMaximizedGroup();
  panel.api.setActive();
  if (compactLayout) dockApi.maximizeGroup(panel);
}

const VIEWER_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "pdf", "csv", "tsv", "html", "htm", "docx"]);

export function isViewerFile(path: string): boolean {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return VIEWER_EXTS.has(ext);
}
