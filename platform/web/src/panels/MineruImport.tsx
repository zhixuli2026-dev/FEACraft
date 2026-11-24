import { useRef, useState } from "react";
import { api } from "../api";
import { activatePanel } from "../shell/dock";
import { captureProjectCase, useStore } from "../store";

type Result = { markdown_path: string; assets: number; reused: boolean };

export default function MineruImport({ caseId, readOnly }: { caseId: number; readOnly: boolean }) {
  const files = useStore(state => state.fileTree);
  const [source, setSource] = useState("");
  const [member, setMember] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  async function importBundle() {
    const selection = captureProjectCase();
    if (selection?.caseId !== caseId || !source || readOnly || inFlight.current) return;
    inFlight.current = true; setBusy(true); setResult(null); setMessage("");
    try {
      const imported = await api.post<Result>(`/cases/${caseId}/mineru:import`,
        { path: source, markdown_member: member.trim() || undefined });
      if (!selection.isCurrent()) return;
      setResult(imported);
      void useStore.getState().loadTree().catch(() => {});
    } catch (error) {
      if (selection.isCurrent()) setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      inFlight.current = false;
      if (selection.isCurrent()) setBusy(false);
    }
  }

  async function openDocument() {
    const selection = captureProjectCase();
    if (selection?.caseId !== caseId || !result) return;
    if (await useStore.getState().openFile(result.markdown_path) && selection.isCurrent()) activatePanel("editor");
  }

  return <details className="vf-tikz">
    <summary>导入 MinerU 文档</summary>
    <div className="vf-tikz-body">
      <label>算例 ZIP<select aria-label="算例 ZIP" value={source} disabled={busy || readOnly}
        onChange={event => { setSource(event.target.value); setResult(null); setMessage(""); }}>
        <option value="">选择已上传的 ZIP</option>
        {files.filter(file => file.kind !== "directory" && /\.zip$/i.test(file.path))
          .map(file => <option key={file.path} value={file.path}>{file.path}</option>)}
      </select></label>
      <label>Markdown 文件（有多份时填写）<input aria-label="压缩包内 Markdown 文件" value={member} disabled={busy || readOnly}
        placeholder="例如 output/markdown.md" onChange={event => setMember(event.target.value)} /></label>
      <div className="vf-tikz-actions">
        <button type="button" onClick={() => void importBundle()} disabled={!source || busy || readOnly}>
          {busy ? "正在导入…" : "导入文档和图片"}
        </button>
        {result && <button type="button" onClick={() => void openDocument()}>打开导入的 Markdown</button>}
      </div>
      {result && <p role="status">{result.reused ? "已打开现有导入记录" : "已导入"} · {result.assets} 个本地图像引用</p>}
      {message && <p role="alert">{message}</p>}
    </div>
  </details>;
}
