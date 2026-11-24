import { useEffect, useRef, useState } from "react";
import { api, ApiError, isCaseReadOnly } from "../api";
import { useStore } from "../store";
import { activatePanel } from "../shell/dock";
import { revealLine } from "../shell/editorRef";
import { resolveLatexSource } from "../utils/latexDiagnostics";
import { parseDelimited } from "../utils/delimited";
import { useCaseDraft } from "../utils/useCaseDraft";
import type { CaseDictSnapshot, DictReceipt } from "../utils/useCaseDraft";
import ReportDerivatives from "./ReportDerivatives";
import { readDerivative, readDerivativeFile, saveDerivative } from "../utils/reportDerivative";
import CaseWorkflowControls from './CaseWorkflowControls';
import ReportBuildControl from './ReportBuildControl';
import TikzEditor from './TikzEditor';
import MineruImport from './MineruImport';
import PptImageRebuild from './PptImageRebuild';
import PptVideoExport from './PptVideoExport';
import type { PptRebuild } from '../utils/pptImageRebuild';
import CaseErrors from "./CaseErrors";
import CaseExtensions from "./CaseExtensions";
import CaseMetaEditor from "./CaseMetaEditor";
import CasePhysicalFields, { isStructuredPhysicalField } from "./CasePhysicalFields";
import CaseMaterialLibrary from "./CaseMaterialLibrary";
import CaseIntentHistory from "./CaseIntentHistory";
import CaseArtifactTrace from "./CaseArtifactTrace";
import CaseMeshStudy from "./CaseMeshStudy";
import { DraftReview, draftError } from "./CaseDraftFeedback";

/** caseDict 面板：状态机视图 + 结构化预览 + 状态推进（带确认与错误门控） */
export default function CasePanel() {
  const { currentCase, currentWs, user, refreshCase, loadTree } = useStore();
  const [info, setInfo] = useState<(CaseDictSnapshot & {caseId:number}) | null>(null);
  const [err, setErr] = useState("");
  const loadGeneration = useRef(0);

  const load = async () => {
    if (!currentCase) return;
    const caseId = currentCase.id;
    const generation = ++loadGeneration.current;
    const isCurrent = () => generation === loadGeneration.current && useStore.getState().currentCase?.id === caseId;
    try {
      const r = await api.get<CaseDictSnapshot>(`/cases/${caseId}/caseDict`);
      if (isCurrent()) { setInfo({...r, caseId}); setErr(""); }
    } catch (e: any) {
      if (isCurrent()) setErr(e.message);
    }
  };
  useEffect(() => {
    setInfo(null); setErr(""); void load();
    const reconcile = (event:Event) => { if((event as CustomEvent).detail?.caseId===currentCase?.id)void load(); };
    window.addEventListener('vibeflow:case-reconciled',reconcile);
    return()=>{++loadGeneration.current;window.removeEventListener('vibeflow:case-reconciled',reconcile);};
  }, [currentCase?.id]);

  if (!currentCase) return <div style={s.hint}>未选择算例</div>;
  if (!info || info.caseId !== currentCase.id) return <div style={s.hint}>{err ? `caseDict 读取失败：${err}` : "加载中…"}</div>;

  const intent = info.parsed?.intent ?? {};

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 10, color: "#cfd6e4", fontSize: 13 }}>
      {err && <div role="alert" style={{color:"#fca5a5"}}>刷新失败，现有草稿已保留：{err}</div>}
      {user&&<CaseWorkflowControls key={`${user.id}:${currentCase.id}`} caseId={currentCase.id} userId={user.id} snapshot={info}
        onChanged={async()=>{await refreshCase(currentCase.id);await load();await loadTree();}} />}
      <CaseMetaEditor key={`meta-${currentCase.id}`} caseId={currentCase.id} snapshot={info}
        onSaved={async () => {await refreshCase(currentCase.id); await load();}} />
      {currentWs && <CaseMaterialLibrary key={`materials-${currentWs.id}-${currentCase.id}`} workspaceId={currentWs.id}
        caseId={currentCase.id} snapshot={info} readOnly={isCaseReadOnly(currentCase, currentWs)}
        onApplied={async () => {await refreshCase(currentCase.id); await load();}} />}

      <div style={s.h}>结构化编辑（块级表单——只改目标块，保住注释与其它块）</div>
      <BlockEditor key={`block-${currentCase.id}`} caseId={currentCase.id} snapshot={info} onSaved={load} />
      <CaseErrors key={`errors-${currentCase.id}`} caseId={currentCase.id} snapshot={info} onSaved={load} />
      <CaseExtensions key={`extensions-${currentCase.id}`} caseId={currentCase.id} snapshot={info} onSaved={load} />

      <div style={s.h}>intent</div>
      <KV data={intent} />
      <CaseIntentHistory key={`intent-history:${user?.id}:${currentCase.id}`} caseId={currentCase.id} revision={info.dict_revision} />
      <CaseArtifactTrace key={`artifact-trace:${user?.id}:${currentCase.id}`} caseId={currentCase.id} />
      <CaseMeshStudy key={`mesh-study:${user?.id}:${currentCase.id}`} caseId={currentCase.id} />

      <div style={s.h}>报告（E 阶段）</div>
      <ReportSection key={`report-${user?.id}:${currentCase.id}`} caseId={currentCase.id} />
      {user && <ReportBuildControl key={`report-build-${user.id}:${currentCase.id}`} caseId={currentCase.id} userId={user.id} />}
      {user && <ReportDerivatives key={`report-history-${user.id}:${currentCase.id}`} caseId={currentCase.id} userId={user.id} />}

      <div style={s.h}>可编辑 PPTX</div>
      <PptSection key={`ppt-${currentCase.id}`} caseId={currentCase.id} />

      <div style={s.h}>LaTeX 精排（E-report/latex/）</div>
      {user && <LatexSection key={`latex-${user.id}:${currentCase.id}`} userId={user.id} caseId={currentCase.id} />}

      <div style={s.h}>多工况（C-run）</div>
      <RunsSection caseId={currentCase.id} />
      <div style={s.h}>UDF D1–D5</div>
      <UdfSection key={currentCase.id} caseId={currentCase.id} />
    </div>
  );
}

const udfDefaults = {
  functionObject: { name: "myFunctionObject", objective: "首次执行输出 P0_UDF_OK" },
  boundaryCondition: { name: "velocityBoundary", objective: "施加声明的速度边界条件" },
  sourceTerm: { name: "uniformAcceleration", objective: "在动量方程中施加均匀加速度" },
  turbulenceModel: { name: "customTurbulence", objective: "在 RAS 模型中使用指定系数计算湍流黏度" },
  solver: { name: "diffusionSolver", objective: "用自定义求解器计算瞬态热扩散" },
};
type UdfKind = keyof typeof udfDefaults;

function UdfSection({ caseId }: { caseId: number }) {
  const [kind, setKind] = useState<UdfKind>("functionObject");
  const [name, setName] = useState(udfDefaults.functionObject.name);
  const [objective, setObjective] = useState(udfDefaults.functionObject.objective);
  const [runDir, setRunDir] = useState("C-run/case-udf");
  const [stage, setStage] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const monitoring = useRef<AbortController | null>(null);
  useEffect(() => () => monitoring.current?.abort(), []);

  const waitJob = async (jobId: number, signal: AbortSignal) => {
    while (!signal.aborted) {
      const job = await api.get(`/jobs/${jobId}`, undefined, signal);
      if (job.status === "succeeded") return true;
      if (["failed", "cancelled", "interrupted"].includes(job.status)) {
        throw new Error(`UDF 作业 #${jobId} ${job.status}，请查看作业日志`);
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    return false;
  };
  const execute = async (action: "scaffold" | "build" | "integrate") => {
    const monitor = new AbortController();
    monitoring.current = monitor;
    const { signal } = monitor;
    setBusy(true);
    setStatus({ scaffold: "正在生成开发文件…", build: "正在编译并验证运行入口…", integrate: "正在运行原生 OpenFOAM…" }[action]);
    try {
      if (action === "scaffold") {
        await api.post(`/cases/${caseId}/udf/scaffold`, {name, objective, kind});
        if (signal.aborted) return;
        setStage("D1–D3");
        setStatus("D1 需求、D2 设计、D3 源码已生成");
      } else {
        const payload = action === "build" ? {name} : {name, run_dir: runDir};
        let jobId: number;
        try {
          const result = await api.post<{job_id: number}>(`/cases/${caseId}/udf:${action}`, payload, crypto.randomUUID());
          jobId = result.job_id;
        } catch (error) {
          const detail = error instanceof ApiError && error.status === 409
            ? error.detail as {job_id?: number} | null : null;
          if (!detail?.job_id || !Number.isSafeInteger(detail.job_id)) throw error;
          jobId = detail.job_id;
        }
        if (signal.aborted) return;
        setStatus(`${action === "build" ? "正在编译并验证运行入口" : "正在运行原生 OpenFOAM"}（作业 #${jobId}）`);
        if (!await waitJob(jobId, signal)) return;
        if (action === "build") {
          setStage("D4");
          setStatus("D4 编译与运行入口验证通过");
        } else {
          setStage("D5");
          setStatus("D5 原生 OpenFOAM 集成通过");
        }
      }
    } catch (error: any) {
      if (!signal.aborted) setStatus(error?.message ?? String(error));
    } finally {
      if (!signal.aborted) setBusy(false);
      if (monitoring.current === monitor) monitoring.current = null;
    }
  };
  const field: React.CSSProperties = { display: "grid", gap: 4, fontSize: 12 };
  return <div data-testid="udf-section" aria-busy={busy} style={{ display: "grid", gap: 7 }}>
    <label style={field}>UDF 类型
      <select value={kind} disabled={busy} style={s.select} onChange={event => {
        const next = event.target.value as UdfKind;
        if (name === udfDefaults[kind].name) setName(udfDefaults[next].name);
        if (objective === udfDefaults[kind].objective) setObjective(udfDefaults[next].objective);
        setKind(next);
      }}>
        <option value="functionObject">函数对象</option>
        <option value="boundaryCondition">速度边界条件</option>
        <option value="sourceTerm">动量源项</option>
        <option value="turbulenceModel">湍流模型</option>
        <option value="solver">自定义求解器</option>
      </select>
    </label>
    <label style={field}>项目名称
      <input data-testid="udf-name" value={name} disabled={busy} onChange={event => setName(event.target.value)} style={s.input} />
    </label>
    <label style={field}>D1 需求目标
      <input data-testid="udf-objective" value={objective} disabled={busy} onChange={event => setObjective(event.target.value)} style={s.input} />
    </label>
    <label style={field}>运行目录
      <input data-testid="udf-run-dir" value={runDir} disabled={busy} onChange={event => setRunDir(event.target.value)} style={s.input} />
    </label>
    <div style={{ display: "flex", gap: 4, marginTop: 5, flexWrap: "wrap" }}>
      <button data-testid="udf-scaffold" disabled={busy} onClick={() => execute("scaffold")} style={s.miniBtn}>生成 D1–D3</button>
      <button data-testid="udf-build" disabled={busy} onClick={() => execute("build")} style={s.miniBtn}>D4 编译验证</button>
      <button data-testid="udf-integrate" disabled={busy} onClick={() => execute("integrate")} style={s.miniBtn}>D5 集成执行</button>
    </div>
    <div data-testid="udf-stage" style={{ fontSize: 11, color: "var(--vf-accent)" }}>{stage}</div>
    <div data-testid="udf-status" role="status" style={{ fontSize: 12, color: "var(--vf-text)" }}>{status}</div>
  </div>;
}

interface DictFormProps { caseId:number; snapshot:CaseDictSnapshot; onSaved:() => Promise<void> }

/** LaTeX 编译区（E-report/latex/；WSL TeX Live latexmk） */
interface LatexDiagnostic {
  severity: "error" | "warning";
  message: string;
  file: string | null;
  line: number | null;
  context: string;
}

interface LatexSubmission { key: string; main: string; recipe: string; jobId?: number }
const latexDefaultMain = "E-report/latex/main.tex";
function readLatexSubmission(key: string): LatexSubmission | undefined {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (typeof value?.key !== "string" || !/^[0-9a-f-]{36}$/.test(value.key) ||
        typeof value.main !== "string" || !value.main.startsWith("E-report/latex/") || !value.main.endsWith(".tex") ||
        !["xelatex", "pdflatex", "lualatex"].includes(value.recipe) ||
        value.jobId !== undefined && (!Number.isSafeInteger(value.jobId) || value.jobId < 1)) return;
    return value;
  } catch { /* An invalid session hint cannot resume a job. */ }
}

function insertLatexTable(source: string, latex: string) {
  // Consume comments and escaped punctuation first, then use the last document terminator.
  let offset = source.length;
  for (const token of source.matchAll(/%[^\r\n]*|\\(?:[^a-zA-Z\r\n]|end\s*\{document\})/g)) {
    if (token[0].startsWith("\\end")) offset = token.index;
  }
  const lineEnd = source.includes("\r\n") ? "\r\n" : "\n";
  const before = source.slice(0, offset);
  const prefix = before && !before.endsWith("\n") ? before + lineEnd : before;
  return { content: prefix + latex.replaceAll("\n", lineEnd) + lineEnd + source.slice(offset),
    line: prefix.split("\n").length };
}

function LatexSection({ caseId, userId }: { caseId: number; userId: number }) {
  const { fileTree, currentCase, currentWs } = useStore();
  const texFiles = fileTree.filter((t) => t.path.startsWith("E-report/latex/") && t.path.endsWith(".tex"));
  const storageKey = `vf-latex-build-v1:${userId}:${caseId}`;
  const [pending, setPending] = useState<LatexSubmission>();
  const [main, setMain] = useState("");
  const [recipe, setRecipe] = useState("xelatex");
  const [status, setStatus] = useState("");
  const [pdfs, setPdfs] = useState<string[]>([]);
  const [tableHeaders, setTableHeaders] = useState("指标,值");
  const [tableRows, setTableRows] = useState("A,1\nB,2");
  const [tableBooktabs, setTableBooktabs] = useState(true);
  const [tableBorders, setTableBorders] = useState(false);
  const [tableAlign, setTableAlign] = useState("");
  const [tablePreview, setTablePreview] = useState<{ key: string; latex: string }>();
  const [tableStatus, setTableStatus] = useState("");
  const [tableAction, setTableAction] = useState<"generate" | "insert">();
  const tableLock = useRef(false);
  const tableKey = JSON.stringify([tableHeaders, tableRows, tableBooktabs, tableBorders, tableAlign]);
  const preview = tablePreview?.key === tableKey ? tablePreview.latex : "";
  const tablePath = main || "E-report/latex/main.tex";
  const tableReadOnly = isCaseReadOnly(currentCase, currentWs);
  const tableDisabled = !!tableAction || tableReadOnly;
  const [diagnostics, setDiagnostics] = useState<LatexDiagnostic[]>([]);
  const [diagnosticMain, setDiagnosticMain] = useState("E-report/latex/main.tex");
  const [diagnosticError, setDiagnosticError] = useState("");
  const [building, setBuilding] = useState(false);
  const buildLock = useRef(false);
  const buildGeneration = useRef(0);
  const pollTimer = useRef<number | null>(null);

  const remember = (value?: LatexSubmission) => {
    setPending(value);
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* Keep the original request in this panel if tab storage fails. */ }
  };
  const isCurrentBuild = (generation: number) => generation === buildGeneration.current &&
    useStore.getState().user?.id === userId && useStore.getState().currentCase?.id === caseId;

  const loadPdfs = (source: string, signal?: AbortSignal) =>
    api.get<{ outputs: { path: string }[] }>(`/cases/${caseId}/latex/outputs?main=${encodeURIComponent(source)}`, undefined, signal)
      .then((r) => {
        if (!signal?.aborted && useStore.getState().currentCase?.id === caseId) {
          setPdfs(r.outputs.filter((o) => o.path.endsWith(".pdf")).map((o) => o.path));
        }
      })
      .catch(() => {});
  useEffect(() => {
    const generation = ++buildGeneration.current;
    const saved = readLatexSubmission(storageKey);
    if (saved) {
      setPending(saved);
      setMain(saved.main);
      setRecipe(saved.recipe);
      setDiagnosticMain(saved.main);
      if (saved.jobId) {
        setStatus(`重新查询作业 #${saved.jobId}…`);
        buildLock.current = true;
        setBuilding(true);
        void pollBuild(saved.jobId, generation, saved.main);
      } else {
        setStatus("上次编译提交结果待确认，请确认原请求。");
      }
    }
    return () => {
      ++buildGeneration.current;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
      buildLock.current = false;
    };
  }, [storageKey]);
  useEffect(() => {
    const controller = new AbortController();
    setPdfs([]);
    void loadPdfs(main || latexDefaultMain, controller.signal);
    return () => controller.abort();
  }, [main, storageKey]);

  const loadDiagnostics = async (jobId: number, generation: number) => {
    const current = () => isCurrentBuild(generation);
    try {
      const result = await api.get<{ items: LatexDiagnostic[] }>(
        `/cases/${caseId}/latex/errors?job_id=${jobId}`);
      if (!current()) return;
      setDiagnostics(result.items);
      setDiagnosticError("");
    } catch (error: any) {
      if (current()) setDiagnosticError(`诊断读取失败：${error?.message ?? String(error)}`);
    }
  };

  const openDiagnostic = async (item: LatexDiagnostic) => {
    if (!item.line) {
      setDiagnosticError("该诊断没有源行号");
      return;
    }
    const path = resolveLatexSource(item.file, diagnosticMain);
    if (!path) {
      setDiagnosticError("该诊断没有可定位的算例内 .tex 源文件");
      return;
    }
    try {
      if (!await useStore.getState().openFile(path)) return;
      activatePanel("editor");
      revealLine(item.line, path);
      setDiagnosticError("");
    } catch (error: any) {
      setDiagnosticError(`源文件打开失败：${error?.message ?? String(error)}`);
    }
  };

  async function pollBuild(jobId: number, generation: number, source: string) {
    if (!isCurrentBuild(generation)) return;
    try {
      const job = await api.get<{ id: number; case_id: number; command: string; status: string }>(`/jobs/${jobId}`);
      if (!isCurrentBuild(generation)) return;
      if (job.id !== jobId || job.case_id !== caseId || job.command !== "Registered latexmk") {
        remember();
        throw new Error("原作业与当前算例的 LaTeX 编译不匹配");
      }
      if (!["succeeded", "failed", "cancelled", "interrupted"].includes(job.status)) {
        setStatus(`作业 #${jobId} 编译中（独立快照，见作业面板）…`);
        pollTimer.current = window.setTimeout(() => void pollBuild(jobId, generation, source), 2000);
        return;
      }
      if (job.status === "succeeded") {
        setStatus("✓ 编译完成，可从文件树打开 PDF。");
        void loadPdfs(source);
        void useStore.getState().loadTree().catch(() => {});
      } else {
        const message = job.status === "failed" ? "失败（点击下面错误可定位源码）"
          : job.status === "cancelled" ? "已取消" : "已中断";
        setStatus(`✕ 编译${message}`);
      }
      if (job.status === "succeeded" || job.status === "failed") await loadDiagnostics(jobId, generation);
      if (!isCurrentBuild(generation)) return;
      remember();
    } catch (error) {
      if (!isCurrentBuild(generation)) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) remember();
      setStatus(`✕ 无法查询作业 #${jobId}：${error instanceof Error ? error.message : String(error)}`);
    }
    buildLock.current = false;
    setBuilding(false);
  }

  const build = async () => {
    if (buildLock.current) return;
    buildLock.current = true;
    const generation = ++buildGeneration.current;
    const request = pending ?? { key: crypto.randomUUID(), main: main || latexDefaultMain, recipe };
    setBuilding(true);
    setDiagnostics([]);
    setDiagnosticMain(request.main);
    setDiagnosticError("");
    if (request.jobId) {
      setStatus(`重新查询作业 #${request.jobId}…`);
      void pollBuild(request.jobId, generation, request.main);
      return;
    }
    setStatus("提交编译作业…");
    remember(request);
    try {
      const job = await api.post<{ job_id: number }>(`/cases/${caseId}/latex:build`,
        { main: request.main, recipe: request.recipe }, request.key);
      if (!isCurrentBuild(generation)) return;
      if (!Number.isSafeInteger(job.job_id) || job.job_id < 1) throw new Error("未收到有效作业编号");
      remember({ ...request, jobId: job.job_id });
      void pollBuild(job.job_id, generation, request.main);
    } catch (error) {
      if (!isCurrentBuild(generation)) return;
      const detail = error instanceof Error ? error.message : String(error);
      if (error instanceof ApiError && ([400, 401, 403, 404, 422].includes(error.status) || error.code === "input_stale")) {
        remember();
        setStatus(`✕ 编译请求被拒绝：${detail}`);
      } else {
        setStatus(`编译提交结果待确认。请确认原请求。 ${detail}`);
      }
      buildLock.current = false;
      setBuilding(false);
    }
  };

  const runTableAction = async (action: "generate" | "insert") => {
    if (tableLock.current || tableReadOnly) return;
    tableLock.current = true;
    setTableAction(action);
    setTableStatus("");
    const isCurrent = () => {
      const state = useStore.getState();
      return state.currentCase?.id === caseId && state.user?.id === userId;
    };
    const checkDraft = () => {
      const file = useStore.getState().openFiles[tablePath];
      if (file?.dirty || file?.saving) throw new Error("请先完成编辑器中目标文件的保存，再重试插入。表格输入已保留。");
    };
    let backup = "";
    try {
      if (action === "insert") checkDraft();
      let latex = action === "insert" ? preview : "";
      if (!latex) {
        const headers = parseDelimited(tableHeaders, ",");
        const rows = parseDelimited(tableRows, ",");
        const error = headers.errors[0] || rows.errors[0];
        if (error) throw new Error(error);
        latex = (await api.post<{ latex: string }>(`/cases/${caseId}/latex:table`, {
          headers: headers.rows[0] ?? [], rows: rows.rows, booktabs: tableBooktabs, borders: tableBorders,
          align: tableAlign.trim() || null,
        })).latex;
      }
      if (!isCurrent()) return;
      setTablePreview({ key: tableKey, latex });
      if (action === "generate") {
        setTableStatus("表格片段已生成。");
        return;
      }
      checkDraft();
      const source = await api.get<{ content: string; content_sha256:string }>(`/cases/${caseId}/file?path=${encodeURIComponent(tablePath)}`);
      if (!isCurrent()) return;
      checkDraft();
      if (!source.content_sha256) throw new Error("无法确认源文件版本，请刷新后再插入。");
      const backupPath = `${tablePath}.table-backup-${crypto.randomUUID()}.tex`;
      await api.put(`/cases/${caseId}/file`, {path: backupPath, content: source.content, expected_sha256:null});
      backup = backupPath;
      if (!isCurrent()) return;
      checkDraft();
      const { content, line } = insertLatexTable(source.content, latex);
      const receipt = await api.put<{ content_sha256: string }>(`/cases/${caseId}/file`, {
        path: tablePath, content, expected_sha256: source.content_sha256,
      });
      if (!isCurrent()) return;
      const confirmation = `表格已插入 ${tablePath}。原文件已备份为 ${backup}`;
      setTableStatus(confirmation);
      const state = useStore.getState();
      void state.loadTree().catch(() => {});
      try {
        checkDraft();
        if (state.openFiles[tablePath]) {
          await state.inspectServerVersion(tablePath);
          if (!isCurrent()) return;
          checkDraft();
          if (useStore.getState().openFiles[tablePath]?.serverVersion?.content_sha256 !== receipt.content_sha256) {
            throw new Error("插入后服务器版本有变化。");
          }
          state.resolveFileConflict(tablePath, false);
        }
        if (await state.openFile(tablePath)) {
          activatePanel("editor");
          revealLine(line, tablePath);
        }
      } catch (error) {
        if (isCurrent()) setTableStatus(`${confirmation}。编辑器未更新：${error instanceof Error ? error.message : String(error)}`);
      }
    } catch (error) {
      if (!isCurrent()) return;
      const reason = error instanceof ApiError && error.code === "file_conflict"
        ? "源文件已变化，本次未覆盖；表格片段已保留，可查看源文件后重试。"
        : error instanceof Error ? error.message : String(error);
      const label = action === "generate" ? "表格生成失败" : "表格插入失败";
      setTableStatus(`${label}：${reason}${backup ? ` 读取时的源文件备份：${backup}` : ""}`);
    } finally {
      tableLock.current = false;
      setTableAction(undefined);
    }
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <select data-testid="latex-main" aria-label="LaTeX 主文件" style={s.select} value={main}
                disabled={!!pending || building || tableAction === "insert"} onChange={(e) => setMain(e.target.value)}>
          <option value="">main.tex（默认）</option>
          {texFiles.map((t) => <option key={t.path} value={t.path}>{t.path.split("/").pop()}</option>)}
        </select>
        <select data-testid="latex-recipe" aria-label="LaTeX 编译器" style={s.select} value={recipe}
                disabled={!!pending || building} onChange={(e) => setRecipe(e.target.value)}>
          <option value="xelatex">xelatex（中文推荐）</option>
          <option value="pdflatex">pdflatex</option>
          <option value="lualatex">lualatex</option>
        </select>
        <button data-testid="latex-build" data-building={building} disabled={building || tableAction === "insert"}
                style={s.btn} onClick={build}>{building ? "编译中…" : pending?.jobId ? "刷新作业状态"
                  : pending ? "确认原编译提交" : "🧪 编译 LaTeX PDF"}</button>
      </div>
      {diagnostics.length > 0 && (
        <div data-testid="latex-diagnostics" style={{ marginTop: 6, display: "grid", gap: 3 }}>
          <div style={{ fontSize: 11, color: "#aab" }}>
            编译诊断：{diagnostics.filter((item) => item.severity === "error").length} 错误 · {diagnostics.filter((item) => item.severity === "warning").length} 警告
          </div>
          {diagnostics.map((item, index) => (
            <button key={`${item.severity}-${item.file}-${item.line}-${index}`}
                    data-testid="latex-diagnostic" data-severity={item.severity}
                    data-line={item.line ?? ""} data-file={item.file ?? ""}
                    disabled={!item.line} title={item.context}
                    onClick={() => openDiagnostic(item)}
                    style={{ ...s.miniBtn, textAlign: "left", color: item.severity === "error" ? "#ff9b9b" : "#e8c878" }}>
              <b>{item.severity === "error" ? "错误" : "警告"}</b> {item.file || diagnosticMain}{item.line ? `:${item.line}` : ""} — {item.message}
            </button>
          ))}
        </div>
      )}
      {diagnosticError && <div data-testid="latex-diagnostic-error" style={{ color: "#ff9b9b", fontSize: 11 }}>{diagnosticError}</div>}
      <div data-testid="latex-table-editor" style={{ marginTop: 8, display: "grid", gap: 4 }}>
        <input data-testid="latex-table-headers" value={tableHeaders} disabled={tableDisabled} onChange={(e) => setTableHeaders(e.target.value)}
               aria-label="表头（逗号分隔）" />
        <textarea data-testid="latex-table-rows" value={tableRows} disabled={tableDisabled} onChange={(e) => setTableRows(e.target.value)}
                  aria-label="数据行（每行逗号分隔）" style={{ minHeight: 44 }} />
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <label><input data-testid="latex-table-booktabs" type="checkbox" checked={tableBooktabs} disabled={tableDisabled}
                        onChange={(e) => setTableBooktabs(e.target.checked)} /> booktabs</label>
          <label><input data-testid="latex-table-borders" type="checkbox" checked={tableBorders} disabled={tableDisabled}
                        onChange={(e) => setTableBorders(e.target.checked)} /> 边框</label>
          <input data-testid="latex-table-align" value={tableAlign} disabled={tableDisabled} onChange={(e) => setTableAlign(e.target.value)}
                 placeholder="align（可选，如 lc）" style={{ width: 130 }} />
          <button data-testid="latex-table-generate" style={s.btn} disabled={tableDisabled}
                  onClick={() => void runTableAction("generate")}>{tableAction === "generate" ? "生成中…" : "生成表格"}</button>
          <button data-testid="latex-table-insert" style={s.btn} disabled={tableDisabled}
                  onClick={() => void runTableAction("insert")}>
            {tableAction === "insert" ? "插入中…" : `插入 ${tablePath.split("/").at(-1)}（先备份）`}</button>
        </div>
        {tableReadOnly && <div>此工作区仅可查看。</div>}
        {tableBooktabs && !tableReadOnly && <div>booktabs 样式需要在 LaTeX 导言区加载 <code>\usepackage&#123;booktabs&#125;</code>。</div>}
        {preview && <pre data-testid="latex-table-preview" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12 }}>{preview}</pre>}
        {tableStatus && <div data-testid="latex-table-status" role="status" style={{ overflowWrap: "anywhere" }}>{tableStatus}</div>}
      </div>
      <TikzEditor key={`${userId}:${caseId}`} caseId={caseId} readOnly={tableReadOnly}
        onSaved={path => { if (!pending && !building) setMain(path); void useStore.getState().loadTree().catch(() => {}); }} />
      <MineruImport key={`mineru-${userId}:${caseId}`} caseId={caseId} readOnly={tableReadOnly} />
      <div data-testid="latex-status" role="status" style={{ fontSize: 13, color: "#a8b0bf", marginTop: 4 }}>{status}</div>
      {pdfs.map((p) => (
        <div key={p} style={{ fontSize: 12, padding: "2px 0" }}>📕 {p}</div>
      ))}
    </div>
  );
}

/** 块级表单编辑器（对标原版 OpenFoamDictFormFile：表单改→Apply→Reparse） */
const blockRows = (parsed:Record<string,any> | null, block:string):[string,string][] =>
  Object.entries(parsed?.[block] ?? {}).map(([key,value]) =>
    [key, typeof value === "object" ? JSON.stringify(value) : String(value)]);

const numericalExamples = {
  linearSolvers: { T: { solver: "PCG", preconditioner: "DIC", tolerance: 1e-9, relTol: 0 } },
  convergence: { requireLinearConvergence: true },
  timeControl: { endTime: 1, deltaT: 0.01, writeControl: "runTime", writeInterval: 0.5 },
  nonlinear: { algorithm: "PIMPLE", outerCorrectors: 2, correctors: 2, nonOrthogonalCorrectors: 0 },
  stabilization: { momentumConvection: "upwind" },
  decomposition: { method: "simple", subdomains: 2, divisions: [1, 2, 1] },
};

function BlockEditor({caseId, snapshot, onSaved}: DictFormProps) {
  const { loadTree, refreshCase } = useStore();
  const blocks = Object.entries(snapshot.parsed ?? {})
    .filter(([, v]) => typeof v === "object" && v !== null && !Array.isArray(v))
    .map(([k]) => k);
  const [block, setBlock] = useState("caseMeta");
  const form = useCaseDraft(snapshot, blockRows(snapshot.parsed, block), block);
  const rows = form.draft.value;
  const [status, setStatus] = useState("");
  const [hasError, setHasError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const busyRef = useRef(false);

  const setRow = (i: number, k: string, v: string) =>
    form.edit(rows.map((r, j) => (j === i ? [k, v] : r)));

  const save = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true); setHasError(false);
    setStatus("保存中…");
    try {
      const submitted = form.capture();
      const entries: Record<string, any> = {};
      for (const [k, v] of submitted.value) {
        if (!k.trim()) continue;
        if (/^[{[]/.test(v.trim())) {
          try { entries[k.trim()] = JSON.parse(v); continue; } catch { /* Keep legacy bare-value semantics. */ }
        }
        entries[k.trim()] = v;
      }
      const receipt = await api.put<DictReceipt>(`/cases/${caseId}/caseDict/block`, {block, entries, ...submitted.conditions});
      form.accept(submitted, receipt);
      await onSaved();
      await Promise.allSettled([refreshCase(caseId), loadTree()]);
      setStatus(form.isDirty() ? "提交已保存，后续编辑尚未保存" : "已保存（只替换了该块）");
    } catch (error) {
      setHasError(true); setStatus(draftError(error));
    } finally {
      busyRef.current = false; setBusy(false);
    }
  };

  return (
    <div data-testid="block-editor" data-dirty={form.draft.dirty}
         style={{ border: "1px solid #22304a", borderRadius: 8, padding: 8, background: "#10141c" }}>
      <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
        <select aria-label="编辑的字典块" style={s.select} value={block} disabled={busy} onChange={(e) => {
          if (!form.isDirty() || window.confirm("切换字典块会放弃当前块的本地草稿，是否继续？")) {
            setBlock(e.target.value); setStatus(""); setHasError(false);
          }
        }}>
          {blocks.map((b) => <option key={b} value={b}>{b}</option>)}
          {!blocks.length && <option value="caseMeta">caseMeta</option>}
        </select>
        <button style={s.miniBtn} onClick={() => form.edit([...rows, ["", ""]])}>+ 键</button>
        <button style={s.btn} disabled={busy || !form.draft.dirty || form.draft.key !== block} onClick={save}>
          {busy ? "保存中…" : "应用该块"}</button>
      </div>
      {block === "numericalMethod" && <details style={{ marginBottom: 8 }}>
        <summary>数值设置示例</summary>
        <p>添加与算例匹配的设置。求解时会核对原生字典，MPI 工作流也会核对分区数。</p>
        <div style={{display: "flex", flexWrap: "wrap", gap: 6}}>
          {Object.entries(numericalExamples).map(([key, value]) => <button key={key} type="button" style={s.miniBtn}
            disabled={busy || rows.some(([entry]) => entry.trim() === key)}
            onClick={() => form.edit([...rows, [key, JSON.stringify(value)]])}>添加 {key}</button>)}
        </div>
      </details>}
      {block === "mesh" && <label style={{display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 8}}>
        网格质量预设
        <select aria-label="网格质量预设" style={s.select} value={rows.find(([key]) => key === "qualityPreset")?.[1] ?? ""} disabled={busy}
          onChange={event => {
            const remaining = rows.filter(([key]) => key !== "qualityPreset");
            form.edit(event.target.value ? [...remaining, ["qualityPreset", event.target.value]] : remaining);
          }}>
          <option value="">按质量阈值</option>
          {['smoke', 'coarse', 'production', 'meshIndependence'].map(preset => <option key={preset}>{preset}</option>)}
        </select>
      </label>}
      {block === "physicalProblem" && form.draft.key === block &&
        <CasePhysicalFields rows={rows} disabled={busy} onEdit={form.edit}
          temperatureUnit={snapshot.parsed?.caseMeta?.units?.temperature} />}
      {block === "execution" && <div style={{ marginBottom: 8 }}>
        <p>新建工作流选择「按算例设置」时，采用这里的求解方式。可使用 serial、parallel 或 udf。</p>
        <div style={{display: "flex", flexWrap: "wrap", gap: 6}}>
          <button type="button" style={s.miniBtn} disabled={busy || rows.some(([entry]) => entry.trim() === "workflow")}
            onClick={() => form.edit([...rows, ["workflow", JSON.stringify({solveMode: "parallel", subdomains: 2})]])}>
            添加并行执行设置
          </button>
          <button type="button" style={s.miniBtn} disabled={busy || rows.some(([entry]) => entry.trim() === "interactive")}
            onClick={() => form.edit([...rows, ["interactive", JSON.stringify({shell: "wsl"})]])}>
            添加终端默认环境
          </button>
          <button type="button" style={s.miniBtn} disabled={busy || rows.some(([entry, value]) => entry.trim() === "runMode" && value.trim() === "remote")}
            onClick={() => form.edit([...rows.filter(([entry]) => entry.trim() !== "runMode"), ["runMode", "remote"]])}>
            设为远程求解
          </button>
        </div>
      </div>}
      {rows.map(([k, v], i) => {
        if (block === "physicalProblem" && isStructuredPhysicalField(k)) return null;
        const row = <div style={{ display: "flex", gap: 4, marginBottom: 3 }}>
          <input style={{ ...s.input, maxWidth: 140 }} placeholder="键"
                 aria-label={`第 ${i + 1} 项键名`}
                 value={k} onChange={(e) => setRow(i, e.target.value, v)} />
          <input style={s.input} placeholder="值（[ 或 { 开头按 JSON 解析）"
                 aria-label={k ? `${k} 值` : `第 ${i + 1} 项值`}
                 value={v} onChange={(e) => setRow(i, k, e.target.value)} />
          <button style={s.miniBtn} title="删除该键" aria-label={k ? `删除 ${k}` : `删除第 ${i + 1} 项`}
                  onClick={() => form.edit(rows.filter((_, j) => j !== i))}>✕</button>
        </div>;
        if (block === "physicalProblem" && k === "governingModels") return <details key={i}>
          <summary>控制模型原始字段</summary>{row}
        </details>;
        return <div key={i}>{row}</div>;
      })}
      {!rows.length && <div style={{ fontSize: 11, color: "#667" }}>（该块为空——点「+ 键」开始填）</div>}
      <div role={hasError ? "alert" : "status"} style={{fontSize:13, color:hasError ? "#fca5a5" : "#a8b0bf", marginTop:6}}>{status}</div>
      {hasError && <button style={s.miniBtn} disabled={busy || inspecting} onClick={async ()=>{
        setInspecting(true);
        try { await form.inspect(caseId); } catch (error) { setStatus(draftError(error)); }
        finally { setInspecting(false); }
      }}>{inspecting ? "读取中…" : "查看服务器版本"}</button>}
      {form.review && <DraftReview value={form.review.parsed?.[block]} onChoose={keep=>{
        form.resolve(blockRows(form.review?.parsed ?? null, block), keep);
        setStatus(""); setHasError(false); void onSaved();
      }} />}
    </div>
  );
}

/** 多工况派生：复制基准 case 目录（跳时刻/日志），快速开新工况 */
function RunsSection({ caseId }: { caseId: number }) {
  const { fileTree, loadTree } = useStore();
  const runDirs = Array.from(new Set(
    fileTree.filter((t) => t.path.endsWith("/system/controlDict") && t.path.startsWith("C-run/"))
      .map((t) => t.path.slice(0, -"/system/controlDict".length)),
  ));
  const [base, setBase] = useState("");
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");

  const derive = async () => {
    if (!base || !name.trim()) return;
    setStatus("派生中…");
    try {
      const r = await api.post(`/cases/${caseId}/runs:derive`, { base, name: name.trim() });
      setStatus(`✓ 已创建 ${r.created}（${r.files} 个文件）`);
      setName("");
      await loadTree();
    } catch (e: any) {
      setStatus(`✕ ${e.message}`);
    }
  };

  return (
    <div>
      {runDirs.length ? (
        <div style={{ fontSize: 12, color: "#9ab", marginBottom: 4 }}>
          现有：{runDirs.join("、")}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: "#667", marginBottom: 4 }}>（尚无 C-run 求解目录）</div>
      )}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <select style={s.select} value={base} onChange={(e) => setBase(e.target.value)}>
          <option value="">基准目录…</option>
          {runDirs.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <input style={s.input} placeholder="新工况名（如 case-Re1000）"
               value={name} onChange={(e) => setName(e.target.value)} />
        <button style={s.btn} onClick={derive} disabled={!base || !name.trim()}>派生</button>
      </div>
      <div style={{ fontSize: 11, color: "#667", marginTop: 4 }}>{status}</div>
    </div>
  );
}

type PptPreset = { task_id: string; preset: string; label: string; kind: "shape" | "connector" };

function PptSection({ caseId }: { caseId: number }) {
  const fileTree = useStore((state) => state.fileTree);
  const [title, setTitle] = useState("VibeFlow 仿真报告");
  const [bullets, setBullets] = useState("问题与方法\n网格与验证\n结果与限制");
  const [headers, setHeaders] = useState("指标,值");
  const [rows, setRows] = useState("网格,合格\n求解,完成");
  const [image, setImage] = useState("");
  const [template, setTemplate] = useState("");
  const [templateLayout, setTemplateLayout] = useState({ enabled: false, master: 1, layout: 1 });
  const [notes, setNotes] = useState("");
  const [reconstruction, setReconstruction] = useState<PptRebuild | null>(null);
  const [rebuildBusy, setRebuildBusy] = useState(false);
  const [output, setOutput] = useState("E-report/build/vibeflow.pptx");
  const [presets, setPresets] = useState<PptPreset[]>([]);
  const [presetError, setPresetError] = useState("");
  const [preset, setPreset] = useState("");
  const [beautify, setBeautify] = useState(false);
  const [presetFill, setPresetFill] = useState("#3b82f6");
  const [presetStroke, setPresetStroke] = useState("#213a59");
  const [presetBounds, setPresetBounds] = useState({ x: 75, y: 18, width: 14, height: 12 });
  const [status, setStatus] = useState("");
  const [generated, setGenerated] = useState("");
  const [generatedId, setGeneratedId] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const generation = useRef(0);
  const presetRequest = useRef<AbortController | null>(null);
  const selectedPreset = presets.find((item) => item.preset === preset);
  const presetFits = presetBounds.width > 0 && presetBounds.height > 0 && presetBounds.x >= 0 && presetBounds.y >= 0
    && presetBounds.x + presetBounds.width <= 100 && presetBounds.y + presetBounds.height <= 100;
  useEffect(() => {
    generation.current += 1;
    lock.current = false;
    setBusy(false);
    setStatus("");
    setGenerated("");
    setGeneratedId("");
  }, [caseId]);
  useEffect(() => () => { generation.current += 1; }, []);
  useEffect(() => () => { presetRequest.current?.abort(); }, []);

  const loadPresets = async () => {
    if (presets.length || presetRequest.current) return;
    const request = new AbortController();
    presetRequest.current = request;
    setPresetError("");
    try {
      const response = await api.get<{ presets: PptPreset[] }>(`/cases/${caseId}/ppt/presets`, undefined, request.signal);
      if (!request.signal.aborted) setPresets(response.presets);
    } catch (error) {
      if (!request.signal.aborted) setPresetError(error instanceof Error ? error.message : String(error));
    } finally {
      if (presetRequest.current === request) presetRequest.current = null;
    }
  };

  const build = async () => {
    if (lock.current || rebuildBusy || (!reconstruction && selectedPreset && !presetFits)) return;
    lock.current = true;
    const current = ++generation.current;
    setBusy(true);
    setGenerated("");
    setGeneratedId("");
    setStatus("提交 PPTX 作业…");
    const tableHeaders = headers.split(",").map((item) => item.trim()).filter(Boolean);
    const tableRows = rows.split(/\r?\n/).filter((line) => line.trim())
      .map((line) => line.split(",").map((item) => item.trim()));
    const slide: Record<string, unknown> = { title };
    if (notes.trim()) slide.notes = notes;
    if (reconstruction) {
      const { shapes, ...content } = reconstruction;
      slide.reconstruction = { ...content, text_regions: content.text_regions.filter((item) => item.text.trim()) };
      slide.native_shapes = shapes;
    } else {
      slide.bullets = bullets.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      slide.shape = "roundedRectangle";
      if (tableHeaders.length) slide.table = { headers: tableHeaders, rows: tableRows };
      if (image.trim()) slide.image = { path: image.trim() };
      if (beautify) slide.beautify = true;
      if (template.trim() && templateLayout.enabled) {
        slide.template_layout = { master: templateLayout.master - 1, layout: templateLayout.layout - 1 };
      }
      if (selectedPreset) {
        const { x, y, width, height } = presetBounds;
        slide.native_shapes = [{
          preset: selectedPreset.preset, x: x / 100, y: y / 100, width: width / 100, height: height / 100,
          fill: selectedPreset.kind === "shape" ? presetFill : null, stroke: presetStroke,
        }];
      }
    }
    try {
      const job = await api.post<{ job_id: number; output: string }>(`/cases/${caseId}/ppt:build`, {
        output: output.trim(), title, template: reconstruction ? null : template.trim() || null, slides: [slide],
      });
      let terminal: any = null;
      for (let i = 0; i < 240; i++) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        if (current !== generation.current) return;
        const state = await api.get<{ status: string; log_backlog?: string }>(`/jobs/${job.job_id}`);
        if (["succeeded", "failed", "cancelled", "interrupted"].includes(state.status)) {
          terminal = state;
          break;
        }
      }
      if (!terminal) throw new Error("PPTX 作业等待超时");
      if (terminal.status !== "succeeded") {
        throw new Error(`作业${terminal.status}：${(terminal.log_backlog ?? "").slice(-300)}`);
      }
      if (current !== generation.current) return;
      const publicationId = terminal.attempt?.outputs?.publication?.id;
      if (!publicationId) throw new Error("作业未返回可核验的 PPT 发布版本，请刷新历史列表。");
      setGeneratedId(publicationId);
      setGenerated(job.output);
      setStatus(`✓ PPTX 已生成：${job.output}`);
      await useStore.getState().loadTree();
    } catch (error: any) {
      if (current === generation.current) setStatus(`✕ ${error?.message ?? String(error)}`);
    } finally {
      if (current === generation.current) {
        lock.current = false;
        setBusy(false);
      }
    }
  };

  const download = async () => {
    if (!generated || !generatedId) return;
    const current = generation.current;
    try {
      const document = await readDerivative(caseId,generatedId);
      const file = document.files.find(item=>item.role==="original" && item.path===generated);
      if (document.family!=="ppt" || !file) throw new Error("PPT 原件与本次生成记录不一致。");
      const bytes = await readDerivativeFile(caseId,generatedId,file);
      if (current !== generation.current) return;
      saveDerivative(bytes,generated);
      setStatus(`已开始下载 ${generated}`);
    } catch (error: any) {
      if (current === generation.current) setStatus(`✕ 下载失败：${error?.message ?? String(error)}`);
    }
  };

  return (
    <div data-testid="ppt-editor" style={{ display: "grid", gap: 4 }}>
      <input data-testid="ppt-title" value={title} onChange={(event) => setTitle(event.target.value)} aria-label="PPT 标题" />
      <PptImageRebuild caseId={caseId} imagePaths={fileTree.filter((item) => /\.(png|jpe?g|webp)$/i.test(item.path)).map((item) => item.path)}
        value={reconstruction} onChange={setReconstruction} onBusy={setRebuildBusy} />
      {!reconstruction && <>
      <textarea data-testid="ppt-bullets" value={bullets} onChange={(event) => setBullets(event.target.value)}
                aria-label="PPT 每行一个要点" style={{ minHeight: 48 }} />
      <div style={{ display: "flex", gap: 4 }}>
        <input data-testid="ppt-table-headers" value={headers} onChange={(event) => setHeaders(event.target.value)} aria-label="PPT 表头" />
        <textarea data-testid="ppt-table-rows" value={rows} onChange={(event) => setRows(event.target.value)}
                  aria-label="PPT 表格行" style={{ minHeight: 44, flex: 1 }} />
      </div>
      <input data-testid="ppt-image" value={image} onChange={(event) => setImage(event.target.value)}
               placeholder="可选图片算例路径（png/jpg）" style={s.input} />
      <input data-testid="ppt-template" value={template} onChange={(event) => setTemplate(event.target.value)}
               placeholder="可选 .pptx 模板路径" style={s.input} />
      {template.trim() && <div style={{ display: "grid", gap: 5 }}>
        <label><input type="checkbox" checked={templateLayout.enabled} onChange={(event) =>
          setTemplateLayout({ ...templateLayout, enabled: event.target.checked })} /> 使用模板版式</label>
        {templateLayout.enabled && <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 6 }}>
          <label style={{ display: "grid" }}>母版编号<input type="number" min={1} max={256}
            value={templateLayout.master} onChange={(event) => setTemplateLayout({ ...templateLayout, master: Number(event.target.value) })}
            style={{ ...s.input, minWidth: 0 }} /></label>
          <label style={{ display: "grid" }}>版式编号<input type="number" min={1} max={256}
            value={templateLayout.layout} onChange={(event) => setTemplateLayout({ ...templateLayout, layout: Number(event.target.value) })}
            style={{ ...s.input, minWidth: 0 }} /></label>
        </div>}
      </div>}
      <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input type="checkbox" checked={beautify} onChange={(event) => setBeautify(event.target.checked)} /> 应用 PPT 美化样式
      </label>
      <details onToggle={(event) => { if (event.currentTarget.open) void loadPresets(); }}>
        <summary>添加原生图形</summary>
        <div style={{ display: "grid", gap: 6, paddingBlock: 6 }}>
          <select aria-label="PPT 图形预设" value={preset} onChange={(event) => setPreset(event.target.value)}
                  disabled={!presets.length} style={{ ...s.input, minWidth: 0 }}>
            <option value="">{presets.length ? "选择图形…" : "加载图形目录…"}</option>
            <optgroup label="图形">
              {presets.filter((item) => item.kind === "shape").map((item) =>
                <option key={item.preset} value={item.preset}>{item.label}</option>)}
            </optgroup>
            <optgroup label="连接线">
              {presets.filter((item) => item.kind === "connector").map((item) =>
                <option key={item.preset} value={item.preset}>{item.label}</option>)}
            </optgroup>
          </select>
          {presetError && <div role="alert">{presetError} <button type="button" style={s.miniBtn} onClick={loadPresets}>重试</button></div>}
          {selectedPreset && <>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              {selectedPreset.kind === "shape" && <label>填充 <input type="color" value={presetFill}
                onChange={(event) => setPresetFill(event.target.value)} /></label>}
              <label>描边 <input type="color" value={presetStroke}
                onChange={(event) => setPresetStroke(event.target.value)} /></label>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 6 }}>
              {([ ["x", "左边距"], ["y", "上边距"], ["width", "宽度"], ["height", "高度"] ] as const).map(([key, label]) =>
                <label key={key} style={{ display: "grid", gap: 3 }}>{label} %
                  <input type="number" min={0} max={100} value={presetBounds[key]}
                    onChange={(event) => setPresetBounds((previous) => ({ ...previous, [key]: Number(event.target.value) }))}
                    style={{ ...s.input, minWidth: 0 }} />
                </label>)}
            </div>
            {!presetFits && <div role="alert">图形必须位于页面内，宽度和高度需大于 0。</div>}
          </>}
        </div>
      </details>
      </>}
      <textarea aria-label="演讲者备注" value={notes} onChange={(event) => setNotes(event.target.value)}
                placeholder="演讲者备注（可选）" style={{ ...s.input, minHeight: 44 }} />
      <input data-testid="ppt-output" value={output} onChange={(event) => setOutput(event.target.value)}
               placeholder="输出 .pptx 路径" style={s.input} />
      <div style={{ display: "flex", gap: 5 }}>
        <button data-testid="ppt-build" style={s.btn}
                disabled={busy || rebuildBusy || !title.trim() || !output.trim()
                  || Boolean(!reconstruction && selectedPreset && !presetFits)} onClick={build}>
          {busy ? "生成中…" : "生成可编辑 PPTX"}
        </button>
        {generated && <button data-testid="ppt-download" style={s.miniBtn} onClick={download}>下载</button>}
      </div>
      <div data-testid="ppt-status" style={{ fontSize: 11, color: "#667" }}>{status}</div>
      <PptVideoExport caseId={caseId} recent={generated} sources={fileTree.filter((item) => /\.pptx$/i.test(item.path)).map((item) => item.path)} />
    </div>
  );
}

function ReportSection({ caseId }: { caseId: number }) {
  const [status, setStatus] = useState("");

  const compose = async (overwrite = false) => {
    setStatus("组织报告章节…");
    try {
      const result = await api.post<{ chapters: string[]; backup?: string }>(`/cases/${caseId}/report:compose`,
        { output: "E-report/report.md", overwrite });
      setStatus(`已生成 ${result.chapters.length} 章，含证据链接${result.backup ? "；旧稿已备份" : ""}`);
    } catch (error: any) {
      if (!overwrite && error?.message?.includes("409") &&
          window.confirm("已有 report.md。生成前会备份旧稿并覆盖，继续吗？")) {
        await compose(true);
      } else {
        setStatus(`✕ ${error?.message ?? String(error)}`);
      }
    }
  };

  return (
    <div>
      <button data-testid="report-compose" style={s.btn} onClick={() => compose()}>🧭 组织报告章节</button>
      <div style={{ fontSize: 11, color: "#667", margin: "4px 0" }}>{status}</div>
    </div>
  );
}

function KV({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (!entries.length) return <div style={{ color: "#667", fontSize: 12 }}>（空 — 待澄清需求后填写）</div>;
  return (
    <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12 }}>
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td style={s.tk}>{k}</td>
            <td style={s.tv}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const s: Record<string, React.CSSProperties> = {
  h: { fontSize: 11, color: "#667", textTransform: "uppercase", margin: "12px 0 4px" },
  hint: { padding: 16, color: "#8b93a7", fontSize: 13 },
  btn: { padding: "4px 12px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  miniBtn: { padding: "3px 8px", background: "#1a1e27", color: "#9ab", border: "1px solid #2a2f3a", borderRadius: 5, cursor: "pointer", fontSize: 11 },
  select: { padding: "3px 6px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 5, fontSize: 12 },
  input: { flex: 1, minWidth: 120, padding: "3px 8px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 5, fontSize: 12 },
  tk: { border: "1px solid #2a2f3a", padding: "3px 6px", color: "#9ab", whiteSpace: "nowrap" },
  tv: { border: "1px solid #2a2f3a", padding: "3px 6px", wordBreak: "break-all" },
};
