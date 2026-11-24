import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { useStore } from "../store";
import JobRecovery from "./JobRecovery";
import WorkflowRuns from "./WorkflowRuns";
import ExecutionLauncher from './ExecutionLauncher';
import JobBatches from './JobBatches';
import JobLogView from './JobLogView';
import JobSolverResults from './JobSolverResults';
import JobPostprocessEvidence from './JobPostprocessEvidence';
import RemoteJobs from './RemoteJobs';
import ResidualChart, {type ParsedLog} from './ResidualChart';
import { isNativeSolver } from '../utils/executionLaunch';
import { appendResiduals, type ResidualCursor, type ResidualDelta, type ResidualHistory } from '../utils/jobResidualLog';
import { readCaseSelection, saveCaseSelection } from '../utils/viewSelection';

type NativePolicy = { cpu_limit_ns: number; memory_limit_bytes: number };
type NativeUsage = { cpu_used_ns?: number; memory_and_swap_peak_bytes?: number };
type WindowsUsage = { policy: { user_cpu_seconds: number; committed_memory_bytes: number }; violation: string | null;
  cpu_user_100ns: number; peak_job_memory_bytes: number };

interface Job {
  id: number; case_id: number; type: string; command: string; status: string;
  exit_code: number | null; created_at: string; ended_at: number | null;
  log_path?: string | null;
  phase?: string | null; run_id?: string | null; step_id?: string | null; step_key?: string | null;
  completion_pending?: boolean; completion_error?: string | null;
  attempt?: { id: string; number: number; root_job_id: number; phase: string;
    reason_code: string; retry_of_attempt_id: string | null;
    handler?: { key: string };
    windows_compute_usage?: WindowsUsage;
    wsl_compute_usage?: NativeUsage & { policy: NativePolicy; violation: string | null };
    linux_compute_usage?: { scope: { policy: NativePolicy }; usage: NativeUsage; violation: string | null };
    outputs?: { publication?: { id: string; status: string } } } | null;
  recovery?: { state: string; reason: string; next_step: string } | null;
}

interface JobBatch {
  id: string; status: string; total: number; active: number; max_concurrent: number;
  counts: Record<"queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted", number>;
  jobs: Job[];
}

const STATUS_COLOR: Record<string, string> = {
  queued: "#8b93a7", running: "#3b82f6", succeeded: "#16a34a",
  failed: "#ef4444", cancelled: "#f59e0b", interrupted: "#f97316",
};
const STATUS_TEXT: Record<string, string> = {
  queued: "排队", running: "运行中", succeeded: "成功", failed: "失败",
  cancelled: "已取消", interrupted: "已中断",
};
const PHASE_TEXT: Record<string, string> = {
  ...STATUS_TEXT, claimed: "已领取，等待启动", starting: "启动中", finalizing: "收尾中",
  cancelling: "正在取消",
};

const usageNumber = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });
const computeViolations: Record<string, string> = {
  cpu_limit: "累计 CPU 超限", memory_limit: "内存超限", compute_unverified: "用量待核验",
};

function ComputeUsage({ attempt }: { attempt: NonNullable<Job["attempt"]> }) {
  const { linux_compute_usage: linux, wsl_compute_usage: wsl, windows_compute_usage: windows } = attempt;
  if (!linux && !wsl && !windows) return null;
  const policy = linux?.scope.policy ?? wsl?.policy;
  const native = linux?.usage ?? wsl;
  const cpu = native?.cpu_used_ns ?? windows?.cpu_user_100ns;
  const scale = native ? 1e9 : 1e7;
  const memory = native?.memory_and_swap_peak_bytes ?? windows?.peak_job_memory_bytes;
  const cpuLimit = policy ? policy.cpu_limit_ns / 1e9 : windows?.policy.user_cpu_seconds;
  const memoryLimit = policy?.memory_limit_bytes ?? windows?.policy.committed_memory_bytes;
  const violation = linux?.violation ?? wsl?.violation ?? windows?.violation;
  function quantity(value: number, limit: number | undefined, unit: string) {
    return `${usageNumber.format(value)} ${unit}${limit ? ` / ${usageNumber.format(limit)} ${unit}` : ""}`;
  }
  return <section aria-label="作业资源用量" style={s.usage}>
    <strong>资源用量 · {linux ? "Linux" : wsl ? "WSL" : "Windows"}</strong>
    {cpu !== undefined && <div>{native ? "累计 CPU" : "用户态 CPU"} {quantity(cpu / scale, cpuLimit, "s")}</div>}
    {memory !== undefined && <div>{native ? "内存与交换峰值" : "内存峰值"} {quantity(memory / 1048576,
      memoryLimit === undefined ? undefined : memoryLimit / 1048576, "MiB")}</div>}
    {violation && <strong style={{ color: "#fca5a5" }}>{computeViolations[violation] ?? "资源用量异常"}</strong>}
  </section>;
}

function JobMetadata({ job }: { job: Job }) {
  const phase = job.phase ? PHASE_TEXT[job.phase] ?? job.phase : STATUS_TEXT[job.status] ?? job.status;
  return <details data-testid="job-metadata" style={{ ...s.metadata, flexShrink: 0 }}>
    <summary style={{ cursor: "pointer" }}>作业 #{job.id} · {phase}
      {job.attempt && ` · 第 ${job.attempt.number} 次尝试`}
      {job.recovery?.state === "requires_attention" && " · 需要恢复检查"}
    </summary>
    <div style={{ maxHeight: 120, overflow: "auto" }}>
    {job.attempt ? <>
      <div>第 {job.attempt.number} 次尝试 · <span style={s.identifier}>{job.attempt.id}</span></div>
      {job.run_id && <div>流程运行 · <span style={s.identifier}>{job.run_id}</span></div>}
      {job.step_id && <div>步骤 {job.step_key || ""} · <span style={s.identifier}>{job.step_id}</span></div>}
      {job.attempt.retry_of_attempt_id && <div>重试自 · <span style={s.identifier}>{job.attempt.retry_of_attempt_id}</span></div>}
      {job.attempt.reason_code && <div>结果原因：{job.attempt.reason_code}</div>}
    </> : <div>{job.attempt === null ? "历史作业：未记录流程与尝试信息" : "此记录未提供流程与尝试信息"}</div>}
    {job.completion_pending && <div role="status">正在确认结果发布，收尾完成后才会显示成功。</div>}
    {job.completion_error && <div role="status">收尾尚未完成：{job.completion_error}</div>}
    {job.recovery?.state === "requires_attention" && <div role="status">需要恢复检查，尚未自动重新运行。
      {job.recovery.reason === "publication_recovery_required" ? "结果发布尚未完成。" : "任务状态需要进一步核验。"}</div>}
    </div>
    {job.attempt && <ComputeUsage attempt={job.attempt} />}
  </details>;
}

function JobResiduals({job}: {job: Job}) {
  const [data, setData] = useState<ParsedLog | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('正在解析日志…');
  const [refresh, setRefresh] = useState(0);
  const path = job.log_path || `.vibeflow/jobs/job-${job.id}.log`;
  const live = job.status === 'queued' || job.status === 'running' || job.phase === 'cancelling';
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let cursor: ResidualCursor | null = null;
    let history: ResidualHistory | null = null;
    setError('');
    setNotice('正在解析日志…');

    async function load() {
      let retry = live;
      let interval = 2000;
      try {
        if (!live) {
          const result = await jobRecoveryRequest<ParsedLog>(
            `/cases/${job.case_id}/logs/parse?path=${encodeURIComponent(path)}`, {signal: controller.signal});
          if (!controller.signal.aborted) setData(result);
          return;
        }
        const query = cursor ? '?' + new URLSearchParams({offset: String(cursor.offset), fingerprint: cursor.fingerprint}) : '';
        const result = await jobRecoveryRequest<({available: true} & ResidualDelta) | {available: false}>(
          `/jobs/${job.id}/log/parse${query}`, {signal: controller.signal});
        if (controller.signal.aborted) return;
        setError('');
        if (!result.available) {
          setNotice('等待日志写入…');
          return;
        }
        history = appendResiduals(history, result);
        cursor = result.cursor;
        setData(history.data);
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && cause.code === 'log_prefix_changed' && cursor) {
          cursor = null;
          history = null;
          setData(null);
          setError('');
          setNotice('日志已更新，正在重新解析…');
          interval = 0;
          return;
        }
        if (cause instanceof ApiError) {
          if ([401, 403, 404].includes(cause.status)) setData(null);
          retry = live && (cause.status >= 500 || [409, 429].includes(cause.status));
        }
        setError(cause instanceof ApiError ? cause.message : '无法读取日志，请检查连接后重试。');
      } finally {
        if (retry && !controller.signal.aborted) timer = window.setTimeout(load, interval);
      }
    }

    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [job.id, job.case_id, path, live, refresh]);

  return <div className="vf-residual-workbench">
    {error && <div className="vf-residual-feedback" role="alert">
      <span>{error}</span>
      <button type="button" onClick={() => setRefresh(value => value + 1)}>重新解析</button>
    </div>}
    {data ? <ResidualChart data={data} /> : !error && <p className="vf-residual-feedback" role="status">{notice}</p>}
  </div>;
}

/** 作业面板：提交命令 / 作业列表 / SSE 实时日志 / 残差曲线 / 取消 */
export default function JobsPanel() {
  const userId = useStore(state => state.user?.id);
  const caseId = useStore(state => state.currentCase?.id);
  return userId && caseId ? <JobsSurface key={`${userId}:${caseId}`} /> : <div style={s.hint}>选择一个算例后管理作业</div>;
}

function JobsSurface() {
  const { currentCase, user, jobPanelRequest } = useStore();
  const [saved] = useState(() => readCaseSelection(user!.id, currentCase!.id));
  const expectedAttempt = useRef(saved.attemptId ?? null);
  const [section, setSection] = useState<'jobs' | 'workflows' | 'batches' | 'remote'>(saved.section ?? 'jobs');
  const [requestedRun, setRequestedRun] = useState<{ caseId: number; runId: string } | null>(
    saved.runId ? { caseId: currentCase!.id, runId: saved.runId } : null);
  const [workflowNavigation, setWorkflowNavigation] = useState(0);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [batches, setBatches] = useState<JobBatch[]>([]);
  const [selectedBatch, setSelectedBatch] = useState<string | null>(saved.batchId ?? null);
  const [active, setActive] = useState<number | null>(saved.jobId ?? null);
  const [actionError, setActionError] = useState(''), [actionNotice, setActionNotice] = useState('');
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  const cancelRequests = useRef(new Set<string>());
  const alive = useRef(true);
  const listRead = useRef<{ controller: AbortController; done: Promise<void> } | null>(null);
  const [view, setView] = useState<"log" | "chart">("log");
  const [detail, setDetail] = useState<Job | null>(null);
  const [detailError, setDetailError] = useState("");
  const [listError, setListError] = useState('');
  const caseIdRef = useRef(currentCase?.id);
  const reloadGeneration = useRef(0);
  caseIdRef.current = currentCase?.id;
  const selectedJob = detail?.id === active ? detail : undefined;
  const attempt = selectedJob?.attempt;
  const handler = attempt?.handler?.key;
  const solver = typeof handler === 'string' && handler.endsWith('.solve') ? handler.slice(0, -'.solve'.length) : undefined;
  const publicationId = attempt?.outputs?.publication?.id;

  function changeSection(value: 'jobs' | 'workflows' | 'batches' | 'remote') {
    saveCaseSelection(user!.id, currentCase!.id, { section: value }); setSection(value);
  }
  function selectJob(id: number) {
    expectedAttempt.current = null;
    saveCaseSelection(user!.id, currentCase!.id, { jobId: id, attemptId: null, section: 'jobs' });
    setActive(id); setDetail(null); changeSection('jobs');
  }
  function openRun(runId: string) {
    saveCaseSelection(user!.id, currentCase!.id, { runId, section: 'workflows' });
    setRequestedRun({ caseId: currentCase!.id, runId });
    setWorkflowNavigation(value => value + 1); changeSection('workflows');
  }
  function selectBatch(id: string) {
    saveCaseSelection(user!.id, currentCase!.id, { batchId: id, section: 'batches' });
    setSelectedBatch(id); changeSection('batches');
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; listRead.current?.controller.abort(); }; }, []);

  const reload = async () => {
    if (!currentCase) return;
    if (listRead.current) return listRead.current.done;
    const controller = new AbortController();
    let finish!: () => void;
    const done = new Promise<void>(resolve => { finish = resolve; });
    const pending = { controller, done }; listRead.current = pending;
    const generation = ++reloadGeneration.current;
    try {
      const [jobRows, batchRows] = await Promise.allSettled([
        jobRecoveryRequest<Job[]>(`/cases/${currentCase.id}/jobs`, { signal: controller.signal }),
        jobRecoveryRequest<JobBatch[]>(`/cases/${currentCase.id}/job-batches`, { signal: controller.signal }),
      ]);
      if (!alive.current || controller.signal.aborted || caseIdRef.current !== currentCase.id || generation !== reloadGeneration.current) return;
      const problems: string[] = [];
      if (jobRows.status === 'fulfilled') setJobs(jobRows.value);
      else {
        if (jobRows.reason instanceof ApiError && [401, 403, 404].includes(jobRows.reason.status)) {
          setJobs([]); setDetail(null);
        }
        problems.push('作业列表暂时无法读取，已有选择保留，正在重试。');
      }
      if (batchRows.status === 'fulfilled') setBatches(batchRows.value);
      else {
        if (batchRows.reason instanceof ApiError && [401, 403, 404].includes(batchRows.reason.status)) setBatches([]);
        problems.push('批次列表暂时无法读取，正在重试。单作业列表独立刷新。');
      }
      setListError(problems.join(' '));
    } catch {} finally { if (listRead.current === pending) listRead.current = null; finish(); }
  };
  useEffect(() => {
    setJobs([]); setBatches([]);
    if (currentCase) {
      reload();
      const t = setInterval(reload, 3000);
      const reconcile = (event: Event) => {
        if ((event as CustomEvent).detail?.caseId === currentCase.id) void reload();
      };
      window.addEventListener("vibeflow:case-reconciled", reconcile);
      return () => {
        ++reloadGeneration.current; listRead.current?.controller.abort(); clearInterval(t);
        window.removeEventListener("vibeflow:case-reconciled", reconcile);
      };
    }
  }, [currentCase?.id]);

  useEffect(() => {
    setDetail(null); setDetailError("");
    if (!active || !currentCase) return;
    let cancelled = false;
    let inFlight = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const value = await jobRecoveryRequest<Job>(`/jobs/${active}`, { signal: controller.signal });
        if (cancelled) return;
        if (value.case_id !== currentCase.id || value.id !== active) throw new Error('所选作业的算例归属无法核验，请重新选择。');
        if (expectedAttempt.current && value.attempt?.id !== expectedAttempt.current) throw new Error('原尝试身份已变化，未自动切换到其他尝试。请重新选择作业核对。');
        expectedAttempt.current = value.attempt?.id ?? null;
        saveCaseSelection(user!.id, currentCase.id, { jobId: active, attemptId: expectedAttempt.current });
        setDetail(value); setDetailError("");
      } catch (failure) {
        if (!cancelled) {
          if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) {
            setDetail(null); setDetailError('原作业已不存在或无权访问，未自动选择其他作业。');
          } else if (failure instanceof Error && !(failure instanceof ApiError) && !(failure instanceof TypeError)) {
            setDetail(null); setDetailError(failure.message);
          } else setDetailError("任务详情暂时无法刷新，显示最近一次记录。");
        }
      } finally { inFlight = false; }
    };
    void refresh();
    const timer = setInterval(refresh, 3000);
    const reconcile = (event: Event) => {
      if ((event as CustomEvent).detail?.caseId === currentCase.id) void refresh();
    };
    window.addEventListener("vibeflow:case-reconciled", reconcile);
    return () => {
      cancelled = true; controller.abort(); clearInterval(timer);
      window.removeEventListener("vibeflow:case-reconciled", reconcile);
    };
  }, [active, currentCase?.id]);

  useEffect(() => {
    if (!jobPanelRequest || jobPanelRequest.caseId !== currentCase?.id) return;
    if (jobPanelRequest.runId) {
      openRun(jobPanelRequest.runId);
    } else if (jobPanelRequest.jobId) {
      selectJob(jobPanelRequest.jobId);
    }
  }, [jobPanelRequest, currentCase?.id]);

  const cancel = async (kind: 'job' | 'batch', id: number | string) => {
    if (!currentCase) return;
    const key = `${kind}:${id}`;
    if (cancelRequests.current.has(key)) return;
    cancelRequests.current.add(key); setCancelling(new Set(cancelRequests.current)); setActionError(''); setActionNotice('');
    try {
      const reply = await jobRecoveryRequest<{ ok: boolean; message?: string }>(
        kind === 'job' ? `/jobs/${id}:cancel` : `/cases/${currentCase.id}/job-batches/${id}:cancel`, { body: {} });
      if (alive.current) setActionNotice(reply.ok ? '取消请求已受理，正在核验停止与收尾状态。'
        : reply.message || '本次没有可取消的作业。');
    } catch (failure) { if (alive.current) setActionError(`取消尚未确认：${jobRecoveryError(failure)}。请刷新核对原任务状态。`); }
    finally {
      cancelRequests.current.delete(key);
      if (alive.current) { setCancelling(new Set(cancelRequests.current)); void reload(); }
    }
  };

  if (!currentCase) return <div style={s.hint}>选择一个算例后管理作业</div>;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 13, color: "#d7dce5", containerType: "inline-size", containerName: "jobs" }}>
      <div className="jobs-section-tabs" aria-label="作业面板视图">
        <button aria-pressed={section === 'workflows'} onClick={() => changeSection('workflows')}>工作流</button>
        <button aria-pressed={section === 'jobs'} onClick={() => changeSection('jobs')}>作业与日志</button>
        <button aria-pressed={section === 'batches'} onClick={() => changeSection('batches')}>批次</button>
        <button aria-pressed={section === 'remote'} onClick={() => changeSection('remote')}>远程集群</button>
        {section === 'jobs' && selectedJob?.run_id && <button onClick={() => openRun(selectedJob.run_id!)}>返回所属流程</button>}
      </div>
      {user && section !== 'remote' && <ExecutionLauncher key={`${user.id}:${currentCase.id}`} userId={user.id} caseId={currentCase.id} caseName={currentCase.name}
        onAccepted={receipt => {
          if (receipt.kind === 'workflow') openRun(String(receipt.id));
          else if (receipt.kind === 'batch') selectBatch(String(receipt.id));
          else selectJob(Number(receipt.id));
          void reload();
        }} />}
      {section !== 'workflows' && section !== 'remote' && listError && <p role="status" style={{ ...s.metadata, margin: 0, fontSize: 14 }}>{listError}</p>}
      {section !== 'remote' && (actionError || actionNotice) && <p role={actionError ? 'alert' : 'status'} style={{ ...s.metadata, margin: 0, fontSize: 14 }}>{actionError || actionNotice}</p>}
      {section === 'remote' && user && <RemoteJobs key={`${user.id}:${currentCase.id}`} caseId={currentCase.id} userId={user.id} />}
      {section === 'batches' && <JobBatches rows={batches} selected={selectedBatch} onSelect={selectBatch} onOpenJob={selectJob}
        onCancel={id => void cancel('batch', id)} cancelling={cancelling} />}
      {section === 'workflows' && user && <WorkflowRuns key={`${user.id}:${currentCase.id}:${workflowNavigation}`} caseId={currentCase.id} userId={user.id}
        initialRun={requestedRun?.caseId === currentCase.id ? requestedRun.runId : null}
        onSelectRun={runId => setRequestedRun({ caseId: currentCase.id, runId })}
        onOpenJob={id => { selectJob(id); void reload(); }} />}
      <div style={{ display: section === 'jobs' ? 'flex' : 'none', flex: 1, minHeight: 0, flexDirection: 'column' }}>
      <div className="vf-job-split" data-view={view}>
        {/* 作业列表 */}
        <div className="vf-job-list">
          {jobs.map((j) => (
            <div key={j.id}
                 style={{ ...s.jobRow, background: active === j.id ? "#1c2740" : "transparent" }}
                 onClick={() => selectJob(j.id)}>
              <span style={{ ...s.badge, background: (STATUS_COLOR[j.status] ?? "#8b93a7") + "22",
                             color: STATUS_COLOR[j.status] ?? "#8b93a7",
                             borderColor: STATUS_COLOR[j.status] ?? "#8b93a7" }}>
                {STATUS_TEXT[j.status] ?? j.status}
              </span>
              <button type="button" aria-pressed={active === j.id} onClick={event => { event.stopPropagation(); selectJob(j.id); }}
                    style={{ ...s.jobSelect, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    title={j.command}>
                #{j.id} {j.command}
                {j.phase && j.phase !== j.status && <span> · {PHASE_TEXT[j.phase] ?? j.phase}</span>}
              </button>
              {["queued", "running"].includes(j.status) && !j.completion_pending && (
                <button style={s.cancelBtn} aria-label={`取消作业 ${j.id}`}
                        disabled={cancelling.has(`job:${j.id}`)}
                        onClick={(e) => { e.stopPropagation(); void cancel('job', j.id); }}>{cancelling.has(`job:${j.id}`) ? '取消中' : '取消'}</button>
              )}
            </div>
          ))}
          {!jobs.length && <div style={s.hint}>还没有作业</div>}
        </div>
        {/* 日志 / 曲线 */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, minHeight: 0, overflow: "auto" }}>
          {selectedJob && <JobMetadata key={selectedJob.id} job={selectedJob} />}
          {selectedJob?.status === 'succeeded' && selectedJob.type === 'postprocess' && user && !selectedJob.completion_pending &&
            <JobPostprocessEvidence key={`${user.id}:${currentCase.id}:${selectedJob.id}:${publicationId ?? 'legacy'}`}
              caseId={currentCase.id} jobId={selectedJob.id} publicationId={publicationId} />}
          {selectedJob?.status === 'succeeded' && user && attempt && typeof publicationId === 'string' && isNativeSolver(solver)
            && <JobSolverResults key={`${user.id}:${currentCase.id}:${publicationId}`} caseId={currentCase.id}
              jobId={selectedJob.id} attemptId={attempt.id} publicationId={publicationId} solver={solver} />}
          {selectedJob && user && <JobRecovery key={`${user.id}:${currentCase.id}:${selectedJob.id}`} jobId={selectedJob.id}
            caseId={currentCase.id} userId={user.id}
            revision={JSON.stringify([selectedJob.status, selectedJob.phase, selectedJob.attempt?.id,
              selectedJob.attempt?.phase, selectedJob.attempt?.reason_code, selectedJob.completion_pending, selectedJob.recovery?.reason])}
            onChanged={() => { setDetail(null); void reload(); }}
            onNavigate={id => { selectJob(id); void reload(); }} />}
          {detailError && <div role="status" style={s.metadata}>{detailError}</div>}
          <div style={s.tabBar}>
            <button style={view === "log" ? s.tabOn : s.tab} onClick={() => setView("log")}>日志</button>
            <button style={view === "chart" ? s.tabOn : s.tab} onClick={() => setView("chart")}>残差曲线</button>
          </div>
          {view === "log" ? (
            selectedJob && user ? <JobLogView key={`${user.id}:${currentCase.id}:${selectedJob.id}:${selectedJob.attempt?.id ?? 'legacy'}`}
              userId={user.id} caseId={currentCase.id} jobId={selectedJob.id} attemptId={selectedJob.attempt?.id ?? null} />
              : <div style={s.hint}>{active ? '正在核对所选作业…' : '选择左侧作业查看日志'}</div>
          ) : selectedJob
            ? <JobResiduals key={`${selectedJob.id}/${selectedJob.attempt?.id ?? ''}`} job={selectedJob} />
            : <div style={s.hint}>{active ? '正在核对所选作业…' : '选择左侧作业查看残差曲线'}</div>}
        </div>
      </div>
      </div>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  metadata: { padding: "8px 12px", color: "#b8c6dc", fontSize: 12, lineHeight: 1.6, overflowWrap: "anywhere", borderBottom: "1px solid #2a2f3a" },
  usage: { display: "grid", rowGap: 2, paddingTop: 6, fontVariantNumeric: "tabular-nums" },
  identifier: { fontFamily: "Consolas, monospace" },
  jobSelect: { background: "transparent", color: "inherit", border: "none", textAlign: "left", minHeight: 36, cursor: "pointer", fontSize: 12 },
  hint: { padding: 16, color: "#8b93a7" },
  bar: { display: "flex", flexWrap: "wrap", gap: 6, padding: 8, borderBottom: "1px solid #2a2f3a" },
  batchPanel: { display: "flex", flexDirection: "column", gap: 5, maxHeight: 210, overflow: "auto", padding: 8, borderBottom: "1px solid #2a2f3a", background: "#10141c" },
  batchInput: { flex: 1, resize: "vertical", padding: "6px 10px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12, fontFamily: "Consolas, monospace" },
  batchRow: { display: "flex", alignItems: "center", gap: 12, fontSize: 11, color: "#9aa4b8" },
  input: { flex: 1, padding: "6px 10px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  btn: { padding: "6px 14px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  jobRow: { display: "flex", alignItems: "center", gap: 6, padding: "6px 8px", cursor: "pointer", borderBottom: "1px solid #1a1e27" },
  badge: { fontSize: 10, padding: "1px 6px", borderRadius: 4, border: "1px solid", whiteSpace: "nowrap" },
  cancelBtn: { background: "#7f1d1d", color: "#fca5a5", border: "none", borderRadius: 4, cursor: "pointer", fontSize: 11, padding: "1px 6px" },
  tabBar: { display: "flex", gap: 2, padding: "4px 8px", borderBottom: "1px solid #1a1e27", background: "#10141c" },
  tab: { padding: "3px 12px", background: "transparent", color: "#667", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12 },
  tabOn: { padding: "3px 12px", background: "#1c2740", color: "#7db4ff", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12 },
};
