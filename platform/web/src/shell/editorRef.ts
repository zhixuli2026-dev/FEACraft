// Monaco 编辑器实例的全局引用（供公式插入和 LaTeX 诊断定位）
import type { editor as MonacoEditor } from "monaco-editor";

let editorInstance: MonacoEditor.IStandaloneCodeEditor | null = null;
let editorPath: string | null = null;
let pendingReveal: { line: number; path?: string } | null = null;

function flushReveal(): boolean {
  if (!pendingReveal || !editorInstance) return false;
  if (pendingReveal.path && pendingReveal.path !== editorPath) return false;
  const { line } = pendingReveal;
  pendingReveal = null;
  editorInstance.setPosition({ lineNumber: line, column: 1 });
  editorInstance.revealLineInCenter(line);
  editorInstance.focus();
  return true;
}

export function registerEditor(inst: MonacoEditor.IStandaloneCodeEditor | null) {
  editorInstance = inst;
  inst?.onDidDispose(() => {
    if (editorInstance === inst) editorInstance = null;
  });
  flushReveal();
}

/** EditorPanel 在活动文件变化后同步路径；等待中的诊断定位随后执行。 */
export function syncEditorPath(path: string | null) {
  editorPath = path;
  flushReveal();
}

/** 定位源行；文件尚未挂载时保留最新请求，由 syncEditorPath/registerEditor 完成。 */
export function revealLine(line: number, path?: string): boolean {
  if (!Number.isInteger(line) || line < 1) return false;
  pendingReveal = { line, path };
  return flushReveal();
}

/** 替换当前选区或在光标处插入文本，作为一项可撤销编辑。 */
export function insertAtCursor(text: string, expectedPath?: string): boolean {
  const editor = editorInstance;
  if (!editor?.getModel() || editor.getRawOptions().readOnly ||
      (expectedPath !== undefined && expectedPath !== editorPath)) return false;
  const sel = editor.getSelection();
  if (!sel) return false;
  editor.pushUndoStop();
  if (!editor.executeEdits("vibeflow-insert", [{ range: sel, text }])) return false;
  editor.pushUndoStop();
  editor.focus();
  return true;
}

/** 只在原模型和选区仍适用时兑现跨请求的插入。 */
export function captureEditorInsertion(path: string): ((text: string) => boolean) | null {
  const editor = editorInstance;
  if (!editor || editorPath !== path || editor.getRawOptions().readOnly) return null;
  const selection = editor.getSelection();
  const model = editor.getModel();
  if (!selection || !model) return null;
  const version = model.getVersionId();
  return text => {
    if (editor !== editorInstance || editor.getModel() !== model || model.getVersionId() !== version ||
        !editor.getSelection()?.equalsSelection(selection)) return false;
    return insertAtCursor(text, path);
  };
}
