import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { activatePanel } from "../shell/dock";
import { openProjectFile } from "../shell/openProjectFile";
import { captureProjectCase, useStore } from "../store";
import "./ParaViewMacros.css";

type Backend = "pvpython" | "pvbatch";
type Submission = { key: string; path: string; backend: Backend };
type MacroView = { path: string; backend: Backend; jobId?: number; jobPath?: string; pending?: Submission };
type MacroJob = {
  id: number; case_id: number; status: string; phase?: string; completion_pending?: boolean;
  attempt?: { handler?: { key?: string } }; recovery?: { state?: string };
};

const directory = ".macros/";
const backendName = (value: unknown): Backend => value === "pvbatch" ? "pvbatch" : "pvpython";
const isScript = (value: unknown): value is string => typeof value === "string" &&
  value.startsWith(directory) && /\.py$/i.test(value) && !/[\\:\x00-\x1f]/.test(value) &&
  value.split("/").every(part => part && part !== "." && part !== "..");
const positiveId = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value > 0;
const describe = (error: unknown) => error instanceof Error ? error.message : String(error);
const isActive = (job: MacroJob) => job.completion_pending || job.phase === "cancelling" ||
  job.status === "queued" || job.status === "running";

function load(storageKey: string): MacroView {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) ?? "null");
    const prior = value?.pending;
    const pending = typeof prior?.key === "string" && /^[0-9a-f-]{36}$/.test(prior.key) && isScript(prior.path)
      ? { key: prior.key, path: prior.path, backend: backendName(prior.backend) } : undefined;
    return { path: isScript(value?.path) ? value.path : "", backend: backendName(value?.backend),
      jobId: positiveId(value?.jobId) ? value.jobId : undefined,
      jobPath: isScript(value?.jobPath) ? value.jobPath : undefined, pending };
  } catch {
    return { path: "", backend: "pvpython" };
  }
}

function status(job: MacroJob) {
  if (job.recovery?.state === "requires_attention") return "需要在作业面板核对恢复状态";
  if (job.completion_pending) return "正在确认输出发布";
  if (job.phase === "cancelling") return "正在取消并等待退出";
  const labels: Record<string, string> = {
    queued: "等待执行", running: "执行中", succeeded: "执行成功", failed: "执行失败", cancelled: "已取消", interrupted: "执行中断",
  };
  return labels[job.status] ?? job.status;
}

export default function ParaViewMacros({ caseId, userId }: { caseId: number; userId: number }) {
  const storageKey = `vf-paraview-macro-v1:${userId}:${caseId}`;
  const [view, setView] = useState(() => load(storageKey));
  const files = useStore(state => state.fileTree);
  const canWrite = useStore(state => Boolean(state.currentWs && state.currentWs.access_role !== "viewer"));
  const dirty = useStore(state => Boolean(state.openFiles[view.path]?.dirty));
  const paths = files.filter(file => file.kind !== "directory" && isScript(file.path)).map(file => file.path);
  const selected = paths.includes(view.path) ? view.path : "";
  const [expanded, setExpanded] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [job, setJob] = useState<MacroJob | null>(null);
  const [pollError, setPollError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const action = useRef(false);
  const jobId = view.jobId;
  const jobIsActive = job?.id === jobId && job !== null && Boolean(isActive(job));

  const update = (patch: Partial<MacroView>) => {
    const next = { ...view, ...patch };
    setView(next);
    try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Keep the selection in memory. */ }
  };
  useEffect(() => {
    if (!expanded || !jobId) return;
    const selection = captureProjectCase();
    if (!selection) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const current = await api.get<MacroJob>(`/jobs/${jobId}`);
        if (disposed || !selection.isCurrent()) return;
        if (current.id !== jobId || current.case_id !== caseId || current.attempt?.handler?.key !== "paraview.run") {
          throw new Error("该作业与当前算例的 ParaView 宏不匹配");
        }
        setJob(current);
        setPollError("");
        if (isActive(current) && current.recovery?.state !== "requires_attention") {
          timer = setTimeout(() => void poll(), 1200);
        } else if (current.status === "succeeded") {
          void useStore.getState().loadTree().catch(() => {});
        }
      } catch (cause) {
        if (!disposed && selection.isCurrent()) setPollError(`读取作业状态失败。 ${describe(cause)}`);
      }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [caseId, expanded, jobId, refresh]);

  const withAction = async (label: string, work: (current: () => boolean) => Promise<void>) => {
    const selection = captureProjectCase();
    if (action.current || !selection || selection.caseId !== caseId || selection.userId !== userId) return;
    action.current = true; setBusy(label); setError(""); setNotice("");
    try { await work(selection.isCurrent); }
    catch (cause) { if (selection.isCurrent()) setError(`${label}失败。 ${describe(cause)}`); }
    finally { action.current = false; if (selection.isCurrent()) setBusy(""); }
  };

  const create = () => withAction("新建宏", async current => {
    const value = name.trim();
    const filename = /\.py$/i.test(value) ? value : value + ".py";
    if (!value || !isScript(directory + filename) || filename.includes("/") || filename === ".py") {
      throw new Error("请输入有效的 Python 文件名");
    }
    const path = directory + filename;
    await api.put(`/cases/${caseId}/file`, { path, content: "from paraview import simple\n", expected_sha256: null });
    if (!current()) return;
    update({ path }); setName("");
    await useStore.getState().loadTree();
    if (current()) await openProjectFile(path);
  });

  const submit = () => withAction("运行宏", async current => {
    const request = view.pending ?? { key: crypto.randomUUID(), path: selected, backend: view.backend };
    if (!request.path) return;
    update({ pending: request });
    try {
      const response = await api.post<{ job_id: number }>(`/cases/${caseId}/paraview:run`,
        { script_path: request.path, backend: request.backend }, request.key);
      if (!current()) return;
      if (!positiveId(response.job_id)) throw new Error("未收到有效的作业编号");
      setJob(null); setPollError(""); update({ pending: undefined, jobId: response.job_id, jobPath: request.path });
    } catch (cause) {
      if (!current()) return;
      if (cause instanceof ApiError && [400, 401, 403, 404, 413, 422, 503].includes(cause.status)) {
        update({ pending: undefined });
        throw cause;
      }
      setError(`提交结果尚未确认。请确认原请求。 ${describe(cause)}`);
    }
  });

  const cancel = () => withAction("取消宏", async current => {
    if (!jobId || !jobIsActive) return;
    await api.post(`/jobs/${jobId}:cancel`, {});
    if (current()) { setNotice("取消请求已提交，等待作业退出。"); setRefresh(value => value + 1); }
  });

  const blocked = Boolean(busy || view.pending);
  return <details className="paraview-macros" data-testid="paraview-macros"
    onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>ParaView 宏</summary>
    <p>从算例的 <code>.macros/</code> 选择 Python 脚本。宏在独立 ParaView 作业中运行，输出可从文件树打开。</p>
    <div className="paraview-macro-controls">
      <label>宏脚本
        <select aria-label="宏脚本" value={selected} disabled={blocked} onChange={event => update({ path: event.target.value })}>
          <option value="">{paths.length ? "选择宏脚本" : "暂无宏脚本"}</option>
          {paths.map(path => <option key={path} value={path}>{path.slice(directory.length)}</option>)}
        </select>
      </label>
      <label>执行后端
        <select aria-label="执行后端" value={view.backend} disabled={blocked}
          onChange={event => update({ backend: backendName(event.target.value) })}>
          <option value="pvpython">pvpython</option><option value="pvbatch">pvbatch</option>
        </select>
      </label>
      <button type="button" disabled={!selected || blocked}
        onClick={() => void withAction("打开脚本", async () => { await openProjectFile(selected); })}>打开脚本</button>
      <button type="button" className="paraview-macro-run"
        disabled={!canWrite || Boolean(busy) || (!view.pending && (!selected || dirty || Boolean(jobId && !job) || jobIsActive))}
        onClick={() => void submit()}>{view.pending ? "确认原提交" : "运行宏"}</button>
      {jobIsActive && canWrite && <button type="button"
        disabled={Boolean(busy) || Boolean(job?.completion_pending) || job?.phase === "cancelling"}
        onClick={() => void cancel()}>取消宏</button>}
    </div>
    {canWrite && <form className="paraview-macro-controls" onSubmit={event => { event.preventDefault(); void create(); }}>
      <label>新宏文件名<input value={name} maxLength={160} disabled={blocked}
        placeholder="例如 view.py" onChange={event => setName(event.target.value)} /></label>
      <button type="submit" disabled={blocked || !name.trim()}>新建宏</button>
    </form>}
    {dirty && <p>请先在编辑器保存宏脚本。</p>}
    {!canWrite && <p>此算例为只读，可查看脚本。</p>}
    {busy && <p role="status">{busy}中…</p>}
    {!busy && notice && jobIsActive && <p role="status">{notice}</p>}
    {error && <p role="alert">{error}</p>}
    {jobId && <div className="paraview-macro-controls">
      <p role="status" data-testid="paraview-macro-job">作业 #{jobId} {view.jobPath?.slice(directory.length)} · {job?.id === jobId ? status(job) : "读取状态中"}</p>
      <button type="button" disabled={!job || job.id !== jobId} onClick={() => {
        useStore.getState().requestJobPanel({ caseId, jobId }); activatePanel("jobs");
      }}>查看作业</button>
      {pollError && <button type="button" onClick={() => setRefresh(value => value + 1)}>刷新状态</button>}
    </div>}
    {pollError && <p role="alert">{pollError}</p>}
  </details>;
}
