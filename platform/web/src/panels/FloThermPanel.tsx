import { useEffect, useId, useState } from "react";
import { api, ApiError, isCaseReadOnly } from "../api";
import { activatePanel } from "../shell/dock";
import { useStore } from "../store";
import { jobRecoveryRequest } from "../utils/jobRecovery";
import FloThermLibraryHistory from "./FloThermLibraryHistory";
import "./FloThermPanel.css";

interface Inspection {
  ok: boolean;
  kind: string | null;
  files: { name: string; format: string; sha256: string; bbox_count: number }[];
  blockers: string[];
  warnings: string[];
}

type Submission = { key: string; jobId?: number } & (
  | { action: "convert"; body: { ir_path: string; settings_path: string | null; output_dir: string } }
  | { action: "build-library"; body: { source_dir: string; destination_dir: string } }
);

interface FloThermJob {
  id: number;
  case_id: number;
  status: string;
  completion_pending: boolean;
  completion_error?: string;
  attempt?: { reason_code: string };
}

const statusLabels: Record<string, string> = {
  queued: "已排队", running: "运行中", cancelling: "正在取消", finalizing: "正在收尾",
  succeeded: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断",
};
const terminalStates = new Set(["succeeded", "failed", "cancelled", "interrupted"]);
const defaultOutput = "C-run/flotherm";
const defaultLibrary = "UDF/flotherm-helper";

function rejectedBeforeAdmission(error: ApiError) {
  if (error.status === 409) {
    return typeof error.detail === "string" || error.code === "output_exists" || error.code === "idempotency_conflict";
  }
  return [400, 401, 403, 404, 413, 422].includes(error.status);
}

function readSubmission(key: string): Submission | null {
  try {
    const receipt = JSON.parse(sessionStorage.getItem(key) ?? "null") as Submission | null;
    if (!receipt || typeof receipt.key !== "string" || !receipt.body ||
        (receipt.jobId !== undefined && !Number.isSafeInteger(receipt.jobId))) return null;
    if (receipt.action === "convert" && typeof receipt.body.ir_path === "string" &&
        typeof receipt.body.output_dir === "string") return receipt;
    if (receipt.action === "build-library" && typeof receipt.body.source_dir === "string" &&
        typeof receipt.body.destination_dir === "string") return receipt;
  } catch { /* Ignore an unreadable local receipt. */ }
  return null;
}

function relativeDirectory(value: string) {
  const path = value.trim().replaceAll("\\", "/").replace(/\/+$/, "");
  if (!path || path.startsWith("/") || /^[a-z]:/i.test(path) ||
      path.split("/").some(part => !part || part === "." || part === "..")) return "";
  return path;
}

function FileSelect({ label, paths, value, change }: {
  label: string; paths: string[]; value: string; change: (path: string) => void;
}) {
  const id = useId();
  return <div className="flotherm-field">
    <label htmlFor={id}>{label}</label>
    <select id={id} value={value} onChange={event => change(event.target.value)}>
      <option value="">{paths.length ? "选择文件或目录" : "没有可选项"}</option>
      {value && !paths.includes(value) && <option value={value} disabled>{value}（已不可用）</option>}
      {paths.map(path => <option key={path}>{path}</option>)}
    </select>
  </div>;
}

export default function FloThermPanel() {
  const userId = useStore(state => state.user?.id);
  const currentCase = useStore(state => state.currentCase);
  if (!currentCase || userId === undefined) {
    return <div className="flotherm-studio"><p>请先在项目面板选择算例。</p></div>;
  }
  return <Studio key={`${userId}:${currentCase.id}`} userId={userId}
    caseId={currentCase.id} caseName={currentCase.name} />;
}

function Studio({ userId, caseId, caseName }: { userId: number; caseId: number; caseName: string }) {
  const fileTree = useStore(state => state.fileTree);
  const projectLoadError = useStore(state => state.projectLoadError?.target === "case");
  const readOnly = useStore(state => isCaseReadOnly(state.currentCase, state.currentWs));
  const receiptKey = `vf:flotherm:submission:${userId}:${caseId}`;
  const endpoint = `/cases/${caseId}/flotherm`;
  const paths = fileTree.filter(entry => entry.kind !== "directory").map(entry => entry.path).sort();
  const raw = paths.filter(path => /\.(ecxml|pdml|xtxmla?)$/i.test(path));
  const json = paths.filter(path => /\.json$/i.test(path));
  const available = new Set(paths);
  const libraryDirectories = paths.filter(path => path.endsWith("/Make/files"))
    .map(path => path.slice(0, -"/Make/files".length))
    .filter(path => available.has(`${path}/Make/options`) &&
      paths.some(file => file.startsWith(`${path}/`) && file.endsWith(".C") && !file.slice(path.length + 1).includes("/")));
  const [inputPaths, setInputPaths] = useState(["", "", ""]);
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [inspectError, setInspectError] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [importPath, setImportPath] = useState("Ref/flotherm-imported.json");
  const [importConfig, setImportConfig] = useState("");
  const [importedPath, setImportedPath] = useState("");
  const [importError, setImportError] = useState("");
  const [importing, setImporting] = useState(false);
  const [ir, setIr] = useState("");
  const [settings, setSettings] = useState("");
  const [output, setOutput] = useState(defaultOutput);
  const [source, setSource] = useState("");
  const [library, setLibrary] = useState(defaultLibrary);
  const [showLibrary, setShowLibrary] = useState(false);
  const [receipt, setReceipt] = useState(() => readSubmission(receiptKey));
  const [job, setJob] = useState<FloThermJob | null>(null);
  const [working, setWorking] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [actionError, setActionError] = useState("");
  const [readError, setReadError] = useState("");
  const [fileError, setFileError] = useState("");
  const jobId = receipt?.jobId;
  const finished = job?.id === jobId && !job?.completion_pending && terminalStates.has(job?.status ?? "");
  const locked = working || Boolean(receipt && !finished);
  const scopeIsCurrent = () => {
    const state = useStore.getState();
    return state.user?.id === userId && state.currentCase?.id === caseId;
  };

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let settled = false;
    async function refresh() {
      try {
        const next = await jobRecoveryRequest<FloThermJob>(`/jobs/${jobId}`, {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]),
        });
        if (controller.signal.aborted) return;
        if (next.case_id !== caseId) throw new Error("作业与当前算例不匹配");
        setJob(next);
        setReadError("");
        if (!settled && !next.completion_pending && terminalStates.has(next.status)) {
          await useStore.getState().loadTree();
          settled = true;
        }
      } catch (error) {
        if (!controller.signal.aborted) setReadError(`作业状态暂时无法更新。${String(error)}`);
      }
      if (!controller.signal.aborted && !settled) timer = window.setTimeout(refresh, 1200);
    }
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [caseId, jobId]);

  function save(next: Submission | null) {
    try {
      if (next) sessionStorage.setItem(receiptKey, JSON.stringify(next));
      else sessionStorage.removeItem(receiptKey);
    } catch { /* Use the in-memory receipt if browser storage is unavailable. */ }
    if (scopeIsCurrent()) setReceipt(next);
  }

  async function inspect() {
    setInspecting(true);
    setInspection(null);
    setInspectError("");
    try {
      const value = await jobRecoveryRequest<Inspection>(`${endpoint}:inspect`, {
        body: { paths: inputPaths.filter(Boolean) }, signal: AbortSignal.timeout(30000),
      });
      if (scopeIsCurrent()) setInspection(value);
    } catch (error) {
      if (scopeIsCurrent()) setInspectError(String(error));
    } finally {
      if (scopeIsCurrent()) setInspecting(false);
    }
  }

  async function importSource(preflight: Inspection, path: string) {
    setImporting(true);
    setImportedPath("");
    setImportError("");
    try {
      const inputs = inputPaths.filter(Boolean);
      const expectedHashes = Object.fromEntries(preflight.files.map((file, index) => [inputs[index], file.sha256]));
      const result = await jobRecoveryRequest<{ content: string }>(`${endpoint}:import`, {
        body: { paths: inputs, expected_hashes: expectedHashes, settings_path: importConfig || null },
        signal: AbortSignal.timeout(30000),
      });
      if (!scopeIsCurrent()) return;
      await api.put(`/cases/${caseId}/file`, { path, content: result.content, expected_sha256: null });
      if (!scopeIsCurrent()) return;
      setImportedPath(path);
      await useStore.getState().loadTree();
      if (scopeIsCurrent()) setIr(path);
    } catch (error) {
      if (scopeIsCurrent()) setImportError(String(error));
    } finally {
      if (scopeIsCurrent()) setImporting(false);
    }
  }

  async function launch(request: Submission) {
    setWorking(true);
    setJob(null);
    setActionError("");
    setReadError("");
    setFileError("");
    setCancelRequested(false);
    save(request);
    try {
      const response = await jobRecoveryRequest<{ job_id: number }>(`${endpoint}:${request.action}`, {
        body: request.body, key: request.key, signal: AbortSignal.timeout(30000),
      });
      if (!Number.isSafeInteger(response.job_id)) throw new Error("响应缺少作业编号");
      save({ ...request, jobId: response.job_id });
    } catch (error) {
      if (error instanceof ApiError) {
        const detail = error.detail as { job_id?: number } | null;
        if (detail?.job_id && Number.isSafeInteger(detail.job_id)) save({ ...request, jobId: detail.job_id });
        else if (rejectedBeforeAdmission(error)) save(null);
      }
      if (scopeIsCurrent()) setActionError(String(error));
    } finally {
      if (scopeIsCurrent()) setWorking(false);
    }
  }

  async function cancel() {
    if (!jobId) return;
    setWorking(true);
    setActionError("");
    try {
      const response = await jobRecoveryRequest<{ ok: boolean; message?: string }>(`/jobs/${jobId}:cancel`, {
        body: {}, signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error(response.message ?? "服务器未接受取消请求");
      if (scopeIsCurrent()) setCancelRequested(true);
    } catch (error) {
      if (scopeIsCurrent()) setActionError(String(error));
    } finally {
      if (scopeIsCurrent()) setWorking(false);
    }
  }

  async function openOutput(path: string) {
    setFileError("");
    try {
      const opened = await useStore.getState().openFile(path);
      if (scopeIsCurrent()) {
        if (opened) activatePanel("editor");
        else setFileError(`无法打开 ${path}。`);
      }
    } catch (error) {
      if (scopeIsCurrent()) setFileError(String(error));
    }
  }

  async function refreshOutputs() {
    setFileError("");
    try {
      await useStore.getState().loadTree();
      if (scopeIsCurrent()) setReadError("");
    } catch (error) {
      if (scopeIsCurrent()) setFileError(`无法刷新项目输出。${String(error)}`);
    }
  }

  const target = receipt?.action === "convert" ? receipt.body.output_dir : receipt?.body.destination_dir;
  const published = finished && job?.status === "succeeded" && target;
  const outputs = published ? paths.filter(path => path.startsWith(`${target}/`)) : [];
  const invalidInputs = inputPaths.filter(Boolean).some(path => !raw.includes(path));
  const importInput = !invalidInputs && inspection?.ok ? inspection : null;
  const needsImportConfig = importInput?.files.some(file => file.format === "PDML") &&
    !importInput.files.some(file => file.format === "ECXML");
  const importDestination = relativeDirectory(importPath);
  const canSaveImport = /\.json$/i.test(importDestination) && !available.has(importDestination) &&
    (importConfig ? json.includes(importConfig) : !needsImportConfig);
  return <div className="flotherm-studio">
    <header className="flotherm-heading"><h1>FloTHERM</h1><span>{caseName}</span></header>
    {readOnly && <p role="status">当前以只读权限查看算例。</p>}
    {projectLoadError && <p role="alert">项目文件加载失败，请在项目面板重试。</p>}
    <section className="flotherm-section" aria-label="输入预检">
      <h2>输入预检</h2>
      <form onSubmit={event => { event.preventDefault(); void inspect(); }}>
        <fieldset disabled={inspecting || importing || readOnly}>
          <div className="flotherm-fields">
            {inputPaths.map((path, index) => <FileSelect key={index}
              label={["FloTHERM 输入文件", "配套文件（可选）", "其他文件（可选）"][index]}
              paths={raw.filter(file => file === path || !inputPaths.includes(file))} value={path}
              change={value => {
                setInputPaths(previous => previous.map((entry, slot) => slot === index ? value : entry));
                setInspection(null);
                setInspectError("");
                setImportError("");
                setImportedPath("");
              }} />)}
          </div>
          {!raw.length && paths.length > 0 && <p>请在项目面板导入 ECXML、PDML 或 XT XML 文件。</p>}
          <button type="submit" disabled={!inputPaths[0] || invalidInputs}>{inspecting ? "检查中…" : "检查输入"}</button>
        </fieldset>
      </form>
      {inspectError && <p className="flotherm-error" role="alert">{inspectError}</p>}
      {inspection && <div className="flotherm-check" role="status">
        <strong>{inspection.ok ? "预检通过" : "预检未通过"}{inspection.kind ? ` · ${inspection.kind}` : ""}</strong>
        <ul className="flotherm-file-list">
          {inspection.files.map(file => <li key={file.name}>
            <span>{file.name}</span><small>{file.format} · {file.bbox_count} 个 bbox · <code title={file.sha256}>{file.sha256.slice(0, 12)}</code></small>
          </li>)}
        </ul>
        {inspection.blockers.map((item, index) => <p className="flotherm-error" key={index}>{item}</p>)}
        {inspection.warnings.map((item, index) => <p key={index}>提示：{item}</p>)}
      </div>}
      {importInput && <form onSubmit={event => {
        event.preventDefault();
        if (canSaveImport && !importing && !readOnly) void importSource(importInput, importDestination);
      }}>
        <fieldset disabled={importing || readOnly}>
          {needsImportConfig && <p>PDML 导入配置需提供计算域和环境温度。</p>}
          <div className="flotherm-fields">
            <label className="flotherm-field">IR 保存路径
              <input value={importPath} onChange={event => setImportPath(event.target.value)} required />
            </label>
            <FileSelect label={needsImportConfig ? "导入配置 JSON" : "导入配置 JSON（可选）"}
              paths={json} value={importConfig} change={setImportConfig} />
          </div>
          {available.has(importDestination) && !importedPath && <p>保存路径已有文件，请填写新路径。</p>}
          <button type="submit" disabled={!canSaveImport}>{importing ? "导入中…" : "导入并生成 IR"}</button>
        </fieldset>
        {importedPath && <p role="status">已生成 {importedPath}</p>}
        {importError && <p className="flotherm-error" role="alert">{importError}</p>}
      </form>}
    </section>

    <section className="flotherm-section" aria-label="IR 转换">
      <h2>IR 转换</h2>
      <p>选择已导入或已有的 FloTHERM IR JSON，生成 OpenFOAM 算例文件。</p>
      <form onSubmit={event => {
        event.preventDefault();
        if (locked || readOnly || !json.includes(ir) || !relativeDirectory(output) ||
            (settings && (!json.includes(settings) || settings === ir))) return;
        void launch({ key: crypto.randomUUID(), action: "convert",
          body: { ir_path: ir, settings_path: settings || null, output_dir: relativeDirectory(output) } });
      }}>
        <fieldset disabled={locked || readOnly}>
          <div className="flotherm-fields">
            <FileSelect label="FloTHERM IR JSON" paths={json} value={ir} change={setIr} />
            <FileSelect label="转换设置 JSON（可选）" paths={json.filter(path => path !== ir)} value={settings} change={setSettings} />
            <label className="flotherm-field">输出目录
              <input value={output} onChange={event => setOutput(event.target.value)} required />
            </label>
          </div>
          {output && !relativeDirectory(output) && <p className="flotherm-error">请填写算例下的新目录，例如 C-run/flotherm。</p>}
          <button type="submit" className="flotherm-primary" disabled={!json.includes(ir) ||
            !relativeDirectory(output) || Boolean(settings && (!json.includes(settings) || settings === ir))}>提交转换</button>
        </fieldset>
      </form>
    </section>

    <details className="flotherm-section" onToggle={event => setShowLibrary(event.currentTarget.open)}>
      <summary>辅助库</summary>
      <form onSubmit={event => {
        event.preventDefault();
        if (locked || readOnly || !libraryDirectories.includes(source) || !relativeDirectory(library)) return;
        void launch({ key: crypto.randomUUID(), action: "build-library",
          body: { source_dir: source, destination_dir: relativeDirectory(library) } });
      }}>
        <fieldset disabled={locked || readOnly}>
          <div className="flotherm-fields">
            <FileSelect label="辅助库源码目录" paths={libraryDirectories} value={source} change={setSource} />
            <label className="flotherm-field">辅助库输出目录
              <input value={library} onChange={event => setLibrary(event.target.value)} required />
            </label>
          </div>
          {!libraryDirectories.length && <p>源码目录需包含 Make/files、Make/options 和 C++ 源文件。</p>}
          {library && !relativeDirectory(library) && <p className="flotherm-error">请填写算例下的新目录，例如 UDF/flotherm-helper。</p>}
          <button type="submit" disabled={!libraryDirectories.includes(source) || !relativeDirectory(library)}>编译辅助库</button>
        </fieldset>
      </form>
      {showLibrary && <FloThermLibraryHistory caseId={caseId}
        latestJob={finished && job?.status === "succeeded" && receipt?.action === "build-library" ? job.id : undefined} />}
    </details>

    {(receipt || actionError) && <section className="flotherm-section" aria-label="作业与输出">
      <h2>作业与输出</h2>
      {receipt && <p role="status" className="flotherm-job-state">
        <strong>{receipt.action === "convert" ? "IR 转换" : "辅助库编译"}{jobId ? ` #${jobId}` : ""}</strong>
        <span>{working && !jobId ? "提交中…" : job?.completion_pending ? "输出发布中" : job
          ? statusLabels[job.status] ?? job.status : jobId ? "读取作业状态…" : "等待确认提交结果"}</span>
      </p>}
      {target && <p className="flotherm-path">输出目录 <code>{target}</code></p>}
      {job?.completion_error && <p className="flotherm-error" role="alert">{job.completion_error}</p>}
      {actionError && <p className="flotherm-error" role="alert">{actionError}</p>}
      {readError && <p className="flotherm-error" role="alert">{readError}</p>}
      <div className="flotherm-actions">
        {receipt && !jobId && !working && <button type="button" disabled={readOnly}
          onClick={() => void launch(receipt)}>用原请求确认提交</button>}
        {jobId && <button type="button" onClick={() => {
          useStore.getState().requestJobPanel({ caseId, jobId });
          activatePanel("jobs");
        }}>打开作业日志</button>}
        {job && !finished && !job.completion_pending && !readOnly &&
          <button type="button" disabled={working || cancelRequested || job.status === "cancelling"}
            onClick={() => void cancel()}>{cancelRequested ? "已请求取消" : "取消作业"}</button>}
      </div>
      {published && <div className="flotherm-output">
        <h3>项目输出文件</h3>
        <ul className="flotherm-file-list">
          {outputs.map(file => <li key={file}><button type="button" title={file}
            onClick={() => void openOutput(file)}>{file.slice(target.length + 1)}</button></li>)}
        </ul>
        {!outputs.length && <p>项目目录暂未显示输出文件。 <button type="button"
          onClick={() => void refreshOutputs()}>刷新输出</button></p>}
        {fileError && <p className="flotherm-error" role="alert">{fileError}</p>}
      </div>}
    </section>}
  </div>;
}
