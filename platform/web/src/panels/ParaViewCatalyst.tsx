import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { captureProjectCase, useStore } from "../store";

type Run = { job_id: number; report_path: string };
type Report = { version: number; files: { path: string; size: number }[] };

export default function ParaViewCatalyst({ caseId }: { caseId: number }) {
  const files = useStore(state => state.fileTree);
  const canWrite = useStore(state => Boolean(state.currentWs && state.currentWs.access_role !== "viewer"));
  const scripts = files.filter(file => file.kind !== "directory" && file.path.toLowerCase().endsWith(".py"));
  const endpoint = `/cases/${caseId}/paraview/catalyst`;
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<number[] | null>(null);
  const [version, setVersion] = useState<number>();
  const [driver, setDriver] = useState("");
  const [state, setState] = useState("");
  const [pending, setPending] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const submitting = useRef<AbortController | null>(null);
  const request = useRef<{ payload: string; key: string } | null>(null);

  useEffect(() => () => submitting.current?.abort(), []);
  useEffect(() => {
    if (!open || versions) return;
    const controller = new AbortController();
    void api.get<{ backends: { pvpython?: number[] } }>(endpoint + "/capabilities", undefined, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        const supported = result.backends.pvpython ?? [];
        setVersions(supported);
        setVersion(supported.at(-1));
      }).catch(cause => { if (!controller.signal.aborted) setError(String(cause.message ?? cause)); });
    return () => controller.abort();
  }, [endpoint, open, retry, versions]);

  useEffect(() => {
    const selection = captureProjectCase();
    if (!run || selection?.caseId !== caseId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const current = () => !controller.signal.aborted && selection.isCurrent();
    const poll = async () => {
      try {
        const job = await api.get<{ case_id: number; status: string; recovery?: { state: string } }>(
          `/jobs/${run.job_id}`, undefined, controller.signal);
        if (!current()) return;
        if (job.case_id !== caseId) throw new Error("作业与当前算例不匹配");
        if (job.recovery?.state === "requires_attention") throw new Error("作业等待恢复，可在作业面板查看详情。");
        if (["queued", "running"].includes(job.status)) {
          timer = setTimeout(() => void poll(), 850);
          return;
        }
        if (job.status === "succeeded") {
          const result = await api.get<Report>(`/cases/${caseId}/raw?path=${encodeURIComponent(run.report_path)}`,
            undefined, controller.signal);
          if (!current()) return;
          setReport(result);
          void useStore.getState().loadTree().catch(() => {});
        } else {
          setError(`Catalyst 作业 #${run.job_id} 已结束。可在作业面板查看日志。`);
        }
        setRun(null);
      } catch (cause) {
        if (current()) setError(String(cause instanceof Error ? cause.message : cause));
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [caseId, retry, run]);

  const busy = pending || Boolean(run);
  const ready = canWrite && version && driver && state && state !== driver && !busy;
  async function submit() {
    const selection = captureProjectCase();
    if (!ready || selection?.caseId !== caseId || submitting.current) return;
    const body = { driver_path: driver, state_path: state, version };
    const payload = JSON.stringify(body);
    if (request.current?.payload !== payload) request.current = { payload, key: crypto.randomUUID() };
    const controller = new AbortController();
    submitting.current = controller;
    setPending(true);
    setError("");
    try {
      const job = await api.post<Run>(endpoint, body, request.current.key, controller.signal);
      if (!controller.signal.aborted && selection.isCurrent()) {
        request.current = null;
        setRun(job);
      }
    } catch (cause) {
      if (!controller.signal.aborted && selection.isCurrent()) setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      submitting.current = null;
      if (!controller.signal.aborted && selection.isCurrent()) setPending(false);
    }
  }

  return <details className="vf-pv-extracts" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Catalyst 原位分析</summary>
    {open && <form className="vf-pv-editor" onSubmit={event => { event.preventDefault(); void submit(); }}>
      {versions?.length ? <div className="vf-pv-fields">
        <label>接口版本<select value={version} disabled={!canWrite || busy}
          onChange={event => setVersion(Number(event.target.value))}>
          {versions.map(value => <option key={value} value={value}>Catalyst {value}</option>)}
        </select></label>
        <label>仿真驱动<select value={driver} disabled={!canWrite || busy} onChange={event => setDriver(event.target.value)}>
          <option value="">选择 Python 文件</option>
          {scripts.filter(file => file.path !== state).map(file => <option key={file.path}>{file.path}</option>)}
        </select></label>
        <label>分析状态<select value={state} disabled={!canWrite || busy} onChange={event => setState(event.target.value)}>
          <option value="">选择 Python 文件</option>
          {scripts.filter(file => file.path !== driver).map(file => <option key={file.path}>{file.path}</option>)}
        </select></label>
      </div> : <p role="status">{versions ? "当前 ParaView 未安装 Catalyst。" : error ? "无法读取 Catalyst 支持情况。" : "正在读取 Catalyst 支持情况…"}</p>}
      {Boolean(versions?.length) && <div className="vf-pv-actions"><button type="submit" disabled={!ready}>
        {pending ? "提交中…" : "运行 Catalyst"}</button></div>}
      {run && <p role="status">Catalyst 作业 #{run.job_id} 正在运行，可在作业面板查看。</p>}
      {report && <><p role="status">Catalyst {report.version} · 已生成 {report.files.length} 个提取文件</p>
        <ul>{report.files.map(file => <li key={file.path}>
          <button type="button" title={file.path} onClick={() => { void useStore.getState().openFile(file.path); }}>
            {file.path.split("/").at(-1)}
          </button>
        </li>)}</ul></>}
      {error && <div role="alert"><p>{error}</p>
        {(!versions || run) && <button type="button" onClick={() => { setError(""); setRetry(value => value+1); }}>重查状态</button>}
      </div>}
    </form>}
  </details>;
}
