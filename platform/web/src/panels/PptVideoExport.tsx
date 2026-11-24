import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { readDerivative, readDerivativeFile, saveDerivative } from "../utils/reportDerivative";
import { useStore } from "../store";

type Publication = { id: string; path: string };
type JobStatus = { status: string; log_backlog?: string; attempt?: { outputs?: { publication?: { id?: string } } } };

export async function waitForPptPublication(jobId: number, timeout: number, signal?: AbortSignal) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const job = await api.get<JobStatus>(`/jobs/${jobId}`, undefined, signal);
    if (["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
      if (job.status !== "succeeded") throw new Error(`作业${job.status}：${(job.log_backlog ?? "").slice(-300)}`);
      const id = job.attempt?.outputs?.publication?.id;
      if (!id) throw new Error("作业未返回可核验的发布版本，请刷新历史列表");
      return id;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 600));
    signal?.throwIfAborted();
  }
  throw new Error("等待作业超时，可在作业列表查看发布进度");
}

export default function PptVideoExport({ caseId, sources, recent }:
  { caseId: number; sources: string[]; recent: string }) {
  const [source, setSource] = useState("");
  const [output, setOutput] = useState("E-report/build/narrated.mp4");
  const [language, setLanguage] = useState("english");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [publication, setPublication] = useState<Publication | null>(null);
  const [preview, setPreview] = useState("");
  const request = useRef<AbortController | null>(null);
  const selected = source || recent;

  useEffect(() => () => { request.current?.abort(); }, []);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  const build = async () => {
    if (!selected || !output.trim() || request.current) return;
    const operation = new AbortController();
    request.current = operation;
    setBusy(true);
    setMessage("正在生成旁白和视频…");
    setPublication(null);
    setPreview("");
    try {
      const job = await api.post<{ job_id: number; output: string }>(`/cases/${caseId}/ppt:video`, {
        input: selected, output: output.trim(), language,
      }, undefined, operation.signal);
      const id = await waitForPptPublication(job.job_id, 1500_000, operation.signal);
      operation.signal.throwIfAborted();
      setPublication({ id, path: job.output });
      setMessage("旁白视频已发布");
      await useStore.getState().loadTree();
    } catch (error) {
      if (!operation.signal.aborted) setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (request.current === operation) request.current = null;
      if (!operation.signal.aborted) setBusy(false);
    }
  };

  const read = async (play: boolean) => {
    if (!publication || request.current) return;
    if (play && preview) return;
    const operation = new AbortController();
    request.current = operation;
    setBusy(true);
    try {
      const { id, path } = publication;
      const version = await readDerivative(caseId, id, operation.signal);
      const item = version.files.find((file) => file.role === "original" && file.path === path);
      if (version.family !== "ppt" || !item) throw new Error("视频与所选版本不一致");
      const bytes = await readDerivativeFile(caseId, id, item, operation.signal);
      operation.signal.throwIfAborted();
      if (play) setPreview(URL.createObjectURL(new Blob([bytes], { type: "video/mp4" })));
      else saveDerivative(bytes, path);
    } catch (error) {
      if (!operation.signal.aborted) setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (request.current === operation) request.current = null;
      if (!operation.signal.aborted) setBusy(false);
    }
  };

  return <details data-testid="ppt-video-export">
    <summary>从 PPT 备注生成旁白视频</summary>
    <div style={{ display: "grid", gap: 6, paddingBlock: 6, fontSize: 12 }}>
      <label style={{ display: "grid", gap: 3 }}>选择 PPTX
        <select value={selected} disabled={busy} onChange={(event) => setSource(event.target.value)}>
          <option value="">选择有演讲者备注的文件…</option>
          {[...new Set([...(recent ? [recent] : []), ...sources])].map((path) => <option key={path}>{path}</option>)}
        </select>
      </label>
      <label style={{ display: "grid", gap: 3 }}>旁白语言
        <select value={language} disabled={busy} onChange={(event) => setLanguage(event.target.value)}>
          <option value="english">英语</option><option value="chinese">普通话</option>
        </select>
      </label>
      <label style={{ display: "grid", gap: 3 }}>视频输出路径
        <input value={output} disabled={busy} onChange={(event) => setOutput(event.target.value)} />
      </label>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <button type="button" disabled={busy || !selected || !output.toLowerCase().trim().endsWith(".mp4")}
                onClick={build}>生成旁白视频</button>
        {publication && <>
          <button type="button" disabled={busy} onClick={() => void read(true)}>预览视频</button>
          <button type="button" disabled={busy} onClick={() => void read(false)}>下载 MP4</button>
        </>}
      </div>
      {message && <div role="status">{message}</div>}
      {preview && <video controls preload="metadata" src={preview} style={{ width: "100%", backgroundColor: "#080e19" }} />}
    </div>
  </details>;
}
