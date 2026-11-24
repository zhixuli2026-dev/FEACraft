import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { activatePanel } from '../shell/dock';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { workflowStatusLabel, workflowTargetLabel } from '../utils/workflowRecovery';
import { confirmationStepText, workflowNextStep, workflowReasonLabel, workflowStepLabel, workflowTerminal } from '../utils/workflowRuns';
import type { WorkflowRunDetail, WorkflowRunSummary } from '../utils/workflowRuns';
import WorkflowInputRecovery from './WorkflowInputRecovery';
import './JobRecovery.css';
import './WorkflowRuns.css';
import { readCaseSelection, saveCaseSelection } from '../utils/viewSelection';

interface RunPage { items: WorkflowRunSummary[]; next_cursor: string | null }
const readError = (failure: unknown) => failure instanceof ApiError ? failure.message
  : failure instanceof TypeError ? '无法连接服务'
  : failure instanceof Error ? failure.message : '读取结果无法核验';
function Diagnostic({ code, detail }: { code: string; detail?: string | null }) {
  if (!code && !detail) return null;
  return <div className="workflow-diagnostic">
    {code && <p>{workflowReasonLabel(code)}</p>}
    <details><summary>原始诊断</summary>{detail && <p>{detail}</p>}{code && <p><code>{code}</code></p>}</details>
  </div>;
}

/** A run owns the step order; its explicit active pointer owns the current attempt. */
export default function WorkflowRuns({ caseId, userId, initialRun, onOpenJob, onSelectRun }: {
  caseId: number; userId: number; initialRun: string | null; onOpenJob: (id: number) => void; onSelectRun: (id: string) => void;
}) {
  const [rows, setRows] = useState<WorkflowRunSummary[]>([]);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(() => initialRun ?? readCaseSelection(userId, caseId).runId ?? null);
  const [run, setRun] = useState<WorkflowRunDetail | null>(null);
  const [listError, setListError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [paging, setPaging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const acting = useRef(false);
  const acceptedRuns = useRef(new Map<string, WorkflowRunSummary>());
  const alive = useRef(true);
  const listLoad = useRef<(older?: boolean) => Promise<void>>(async () => {});
  const selection = useRef(selected);
  const feedback = useRef<HTMLParagraphElement>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  function selectRun(id: string) {
    saveCaseSelection(userId, caseId, { runId: id, section: 'workflows' }); onSelectRun(id);
    selection.current = id; setSelected(id); setRun(null); setDetailError(''); setActionError(''); setNotice('');
  }
  function changeFilter(value: string) {
    setRows([]); setLoading(true); setListError(''); setHasMore(false); setFilter(value);
  }

  useEffect(() => {
    let stopped = false, inFlight = false, cursor: string | null = null, olderLoaded = false;
    const controller = new AbortController();
    const load = async (older = false) => {
      if (inFlight || stopped || (older && !cursor)) return;
      inFlight = true; if (older) setPaging(true);
      try {
        const query = new URLSearchParams({ limit: '20' });
        if (filter) query.set('status', filter);
        if (older && cursor) query.set('cursor', cursor);
        const page = await jobRecoveryRequest<RunPage>(`/cases/${caseId}/workflow-runs-page?${query}`, { signal: controller.signal });
        if (stopped) return;
        const items = page.items.map(row => {
          const accepted = acceptedRuns.current.get(row.id);
          return accepted && accepted.version >= row.version ? accepted : row;
        }).filter(row => !filter || row.status === filter);
        setRows(previous => {
          const ids = new Set(items.map(row => row.id));
          return older ? [...previous, ...items.filter(row => !previous.some(item => item.id === row.id))]
            : [...items, ...(olderLoaded ? previous.filter(row => !ids.has(row.id) && (!filter || row.status === filter)) : [])];
        });
        if (older || !olderLoaded) cursor = page.next_cursor;
        if (older) olderLoaded = true;
        setHasMore(!!cursor); setListError('');
      } catch (failure) {
        if (!stopped) {
          const forbidden = failure instanceof ApiError && [401, 403, 404].includes(failure.status);
          if (forbidden) setRows([]);
          setListError(forbidden ? '流程已不存在或无权访问，请核对权限后刷新。'
            : `流程列表刷新失败：${readError(failure)}。可再次刷新，已有记录保留。`);
        }
      }
      finally { inFlight = false; if (!stopped) { setLoading(false); setPaging(false); } }
    };
    listLoad.current = load; void load();
    const timer = window.setInterval(() => void load(), 3000);
    const reconcile = (event: Event) => { if ((event as CustomEvent).detail?.caseId === caseId) void load(); };
    window.addEventListener('vibeflow:case-reconciled', reconcile);
    return () => { stopped = true; controller.abort(); clearInterval(timer); window.removeEventListener('vibeflow:case-reconciled', reconcile); };
  }, [caseId, filter]);

  useEffect(() => {
    if (!selected) return;
    let stopped = false, inFlight = false;
    const controller = new AbortController();
    const load = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const value = await jobRecoveryRequest<WorkflowRunDetail>(`/workflow-runs/${selected}`, { signal: controller.signal });
        if (value.case_id !== caseId) throw new Error('流程的算例归属不一致，请保留记录并刷新。');
        if (!stopped) { setRun(previous => previous?.id === value.id && previous.version > value.version ? previous : value); setDetailError(''); }
      } catch (failure) {
        if (!stopped) {
          if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) setRun(null);
          setDetailError(`详情刷新失败：${readError(failure)}。请刷新核对后再操作。`);
        }
      }
      finally { inFlight = false; }
    };
    void load(); const timer = window.setInterval(() => void load(), 3000);
    const reconcile = (event: Event) => { if ((event as CustomEvent).detail?.caseId === caseId) void load(); };
    window.addEventListener('vibeflow:case-reconciled', reconcile);
    return () => { stopped = true; controller.abort(); clearInterval(timer); window.removeEventListener('vibeflow:case-reconciled', reconcile); };
  }, [selected, caseId, revision]);

  function refresh() { void listLoad.current(); setRevision(value => value + 1); }
  async function cancel() {
    if (!run || acting.current || recoveryBusy || detailError || run.status === 'cancelling' || workflowTerminal(run.status)) return;
    const runId = run.id; acting.current = true; setBusy(true); setActionError(''); setNotice('');
    try {
      const value = await jobRecoveryRequest<WorkflowRunDetail>(`/workflow-runs/${runId}:cancel`, { body: { expected_version: run.version } });
      if (value.case_id !== caseId || value.id !== runId) throw new Error('取消回执归属无法核验，请刷新原流程。');
      if (alive.current && selection.current === runId) {
        acceptedRuns.current.set(value.id, value);
        setRun(value);
        setRows(previous => filter && value.status !== filter ? previous.filter(row => row.id !== value.id)
          : previous.map(row => row.id === value.id ? value : row));
        setNotice(value.status === 'cancelling' ? '已提交取消，正在等待停止与归档核验。' : `流程${workflowStatusLabel(value.status)}。`);
      }
    } catch (failure) { if (alive.current && selection.current === runId) setActionError(`取消未能确认：${jobRecoveryError(failure)}。请核对刷新后的状态。`); }
    finally { acting.current = false; if (alive.current) { setBusy(false); refresh(); feedback.current?.focus(); } }
  }
  const current = run?.id === selected ? run : null;
  const parent = current?.resumption?.source_run_id ?? current?.recomputation?.source_run_id ?? current?.legacy_migration?.source_run_id;
  return <div className="workflow-runs" data-testid="workflow-runs">
    <aside className="workflow-run-list" aria-label="流程列表">
      <div className="workflow-list-tools">
        <label>流程状态<select aria-label="流程状态" value={filter} onChange={event => changeFilter(event.target.value)}>
          <option value="">全部状态</option>
          {['queued', 'running', 'awaiting_confirmation', 'cancelling', 'succeeded', 'failed', 'cancelled', 'interrupted'].map(value => <option key={value} value={value}>{workflowStatusLabel(value)}</option>)}
        </select></label>
        <button onClick={refresh} disabled={loading || paging}>刷新流程</button>
      </div>
      {listError && <p role="alert" className="workflow-feedback">{listError}</p>}
      {loading && <p role="status" className="workflow-muted workflow-empty">正在读取流程列表…</p>}
      {!loading && !rows.length && !listError && <p className="workflow-muted workflow-empty">{filter ? '当前没有符合此状态的流程。切换状态可查看其他历史。' : '此算例还没有工作流。单独提交的命令可在“作业与日志”中查看。'}</p>}
      {rows.map(row => <button key={row.id} className="workflow-run-select" aria-pressed={selected === row.id}
        data-run-id={row.id} onClick={() => selectRun(row.id)}>
        <span className="workflow-run-title">{workflowTargetLabel(row.target)}流程 <span className="workflow-state">{workflowStatusLabel(row.status)}</span></span>
        <span className="workflow-muted">输入版本 {row.expected_revision} · {row.run_dir}</span>
        <span className="workflow-muted workflow-id">{row.id}</span>
      </button>)}
      {hasMore && <button className="workflow-more" disabled={paging} onClick={() => void listLoad.current(true)}>{paging ? '正在读取…' : '读取更早的流程'}</button>}
    </aside>
    <section className="workflow-run-detail" aria-label="流程详情" aria-busy={busy || recoveryBusy}>
      {!selected ? <p className="workflow-muted workflow-empty">选择左侧流程，查看步骤、当前尝试与取消状态。单个作业的日志和恢复入口保留在“作业与日志”。</p>
        : !current && !detailError ? <p role="status" className="workflow-muted workflow-empty">正在读取流程与步骤…</p> : null}
      {detailError && <p className="workflow-feedback" role="alert">{detailError}</p>}
      {current && <>
        <header className="workflow-detail-heading"><h3>{workflowTargetLabel(current.target)}流程 · {workflowStatusLabel(current.status)}</h3>
          {!workflowTerminal(current.status) && <button disabled={busy || recoveryBusy || !!detailError || current.status === 'cancelling'} onClick={() => void cancel()}>
            {busy ? '提交取消中…' : current.status === 'cancelling' ? '正在取消，请等待' : '取消此流程'}
          </button>}
        </header>
        <p className="workflow-muted workflow-id">{current.id}</p>
        <p className="workflow-muted">输入版本 {current.expected_revision} · {current.run_dir}</p>
        <p role="status">{workflowNextStep(current)}</p>
        <Diagnostic detail={current.detail} code={current.error_code} />
        {parent && <p>来源流程 <button className="workflow-link" disabled={busy || recoveryBusy} onClick={() => selectRun(parent)}>{parent}</button></p>}
        <ol className="workflow-step-list">{current.steps.map(step => {
          const attempt = step.attempts.find(value => value.id === step.active_attempt_id);
          return <li key={step.id} data-step-key={step.key}>
            <div className="workflow-step-heading"><h4>{workflowStepLabel(step)}</h4><span className="workflow-state">{workflowStatusLabel(step.status)}</span></div>
            {step.execution_mode === 'reuse' ? <p className="workflow-muted">复用已核验产物{step.reused_publication ? ` · 发布版本 ${step.reused_publication.generation}` : ''}，未创建新的执行尝试。</p>
              : attempt ? <div className="workflow-attempt"><span>当前第 {attempt.number} 次尝试 · {workflowStatusLabel(attempt.phase)}</span>
                <button disabled={busy || recoveryBusy} onClick={() => onOpenJob(attempt.job_id)}>查看作业 #{attempt.job_id} 日志与恢复</button>
                <span className="workflow-muted workflow-id">{attempt.id}</span>
                {attempt.reason_code && <span>{workflowReasonLabel(attempt.reason_code)}</span>}
                {attempt.exit_code !== null && <span>退出码 {attempt.exit_code}</span>}
                {attempt.completion_error && <p className="workflow-feedback">收尾待核验：{attempt.completion_error}</p>}
              </div> : <p className="workflow-muted">{step.confirmation_state ? confirmationStepText(step)
                : step.key === 'preflight' && step.status === 'succeeded' ? '输入检查已完成，无需启动执行作业。' : '此步骤尚未分配执行尝试。'}</p>}
            <Diagnostic detail={step.detail} code={step.error_code} />
            {step.status === 'awaiting_confirmation' && <button disabled={busy || recoveryBusy} onClick={() => {
              useStore.getState().setWorkflowSelection({ caseId, runId: current.id }); activatePanel('caseinfo');
            }}>前往算例阶段确认</button>}
            {step.attempts.some(value => value.id !== step.active_attempt_id) && <details><summary>此步骤的其他尝试</summary>
              {step.attempts.filter(value => value.id !== step.active_attempt_id).map(value => <p key={value.id}>第 {value.number} 次尝试 · {workflowStatusLabel(value.phase)} <button onClick={() => onOpenJob(value.job_id)}>查看作业 #{value.job_id}</button></p>)}
            </details>}
          </li>;
        })}</ol>
        <details className="job-recovery workflow-run-recovery"><summary>核对输入与关联流程</summary>
          <div className="job-recovery-content"><WorkflowInputRecovery key={current.id} runId={current.id} caseId={caseId} userId={userId}
            disabled={busy || !!detailError} onBusy={setRecoveryBusy} onChanged={refresh} onNavigate={onOpenJob} /></div>
        </details>
      </>}
      <p ref={feedback} tabIndex={-1} className="workflow-feedback" role={actionError ? 'alert' : 'status'}>{actionError || notice}</p>
    </section>
  </div>;
}
