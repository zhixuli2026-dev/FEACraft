import { api } from "../api";
import { beginFileOpen, useStore } from "../store";
import type { FileOpenIntent, FilePreviewRequest } from "../store";
import { activatePanel, isViewerFile } from "./dock";

const RESULT_FILE = /\.(vtk|vtu|vtp|vti|vtr|vts|vtm|xdmf|xmf|exo|e|exodus|stl|series)$/i;

function preview(request: FilePreviewRequest) {
  if (!request.intent.isCurrent()) return false;
  useStore.setState({ filePreviewRequest: request });
  activatePanel(request.kind === "step" ? "geometry" : "viz");
  return true;
}

export async function openProjectFile(path: string, options: {
  hash?: string; original?: boolean; intent?: FileOpenIntent | null;
} = {}) {
  const intent = options.intent ?? beginFileOpen();
  if (!intent?.isCurrent()) return false;
  const extension = path.split(".").at(-1)?.toLowerCase();

  if (!options.original) {
    if (extension === "step" || extension === "stp") return preview({ kind: "step", intent, path });
    if (extension === "msh") {
      const converted = await api.post<{ path: string }>(`/cases/${intent.caseId}/geometry/msh-preview`, { path });
      return preview({ kind: "viz", intent, filePath: converted.path });
    }
    if (extension === "foam") {
      const runDir = path.split("/").slice(0, -1).join("/") || ".";
      return preview({ kind: "viz", intent, runDir });
    }
    if (RESULT_FILE.test(path)) return preview({ kind: "viz", intent, filePath: path });
  }

  if (!await useStore.getState().openFile(path, options.hash, intent) || !intent.isCurrent()) return false;
  activatePanel(!options.original && isViewerFile(path) ? "viewer" : "editor");
  return true;
}
