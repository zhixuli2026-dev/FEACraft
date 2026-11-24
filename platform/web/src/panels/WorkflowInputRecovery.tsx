import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { readPendingWorkflowRecovery, workflowRecoveryKey, workflowStatusLabel, workflowTargetLabel } from '../utils/workflowRecovery';
import type { PendingWorkflowRecovery, RecoveryRun, WorkflowRecoveryBody, WorkflowRecoveryContext } from '../utils/workflowRecovery';
import { CORE_KINDS, dependencyLabel } from '../utils/caseWorkflow';
import type { DependencySnapshot } from '../utils/caseWorkflow';

export default function WorkflowInputRecovery({ runId, caseId, userId, disabled, onBusy, onChanged, onNavigate, onRunNavigate, dependency, active = true, observedRevision, observedRunVersion, observedEventSequence }: {
  runId: string; caseId: number; userId: number; disabled: boolean; onBusy: (busy: boolean) => void;
  onChanged: () => void; onNavigate: (jobId: number) => void;
  onRunNavigate?: (runId:string) => void; dependency?:DependencySnapshot;
  active?:boolean; observedRevision?:number; observedRunVersion?:number; observedEventSequence?:number;
}) {
  const readOnly = useStore(s=>s.currentWs?.access_role==='viewer');
  const [context, setContext] = useState<WorkflowRecoveryContext | null>(null);
  const [selected, setSelected] = useState(runId);
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [pending, setPending] = useState<PendingWorkflowRecovery | null>(null);
  const [pendingProblem, setPendingProblem] = useState(''), [canRevise, setCanRevise] = useState(false);
  const [draft, setDraft] = useState<WorkflowRecoveryBody | null>(null);
  const [target, setTarget] = useState('geometry'), [runDir, setRunDir] = useState('');
  const [geometry, setGeometry] = useState('preview'), [report, setReport] = useState('compose');
  const [solve, setSolve] = useState('serial'), [subdomains, setSubdomains] = useState('2'), [udf, setUdf] = useState('');
  const [timeout, setTimeout] = useState('300'), [logMb, setLogMb] = useState('16');
  const live = useRef(true), mutation = useRef(false);
  const formInitialized = useRef(false), lastObservation = useRef('');
  const reading = useRef<AbortController | null>(null), acting = useRef<AbortController | null>(null);
  const feedback = useRef<HTMLParagraphElement>(null);
  const storageKey = workflowRecoveryKey(userId, caseId, selected);
  const viewKey = `vf-workflow-recovery-view-v1:${userId}:${caseId}:${runId}`;
  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);

  const refresh = useCallback(async (beforeSeq?: number) => {
    reading.current?.abort(); const controller = new AbortController(); reading.current = controller; setLoading(true);
    try {
      const value = await jobRecoveryRequest<WorkflowRecoveryContext>(`/workflow-runs/${selected}/recovery${beforeSeq ? `?before_seq=${beforeSeq}` : ''}`, { signal: controller.signal });
      if (value.run.id !== selected || value.run.case_id !== caseId) throw new Error('流程归属无法核验');
      if (live.current && !controller.signal.aborted) {
        setContext(previous => beforeSeq && previous?.run.id === value.run.id
          ? { ...value, successors: { ...value.successors, items: [...previous.successors.items, ...value.successors.items] } } : value);
        if (!beforeSeq && !formInitialized.current) {
          setTarget(value.run.target); setRunDir(value.run.run_dir);
          setGeometry(String(value.run.parameters.geometry_mode ?? 'preview')); setReport(String(value.run.parameters.report_mode ?? 'compose'));
          setSolve(String(value.run.parameters.solve_mode ?? 'serial')); setSubdomains(String(value.run.parameters.subdomains ?? 2));
          setUdf(String(value.run.parameters.udf_name ?? '')); setTimeout(String(value.budget?.timeout_seconds ?? 300));
          setLogMb(String((value.budget?.max_log_bytes ?? 16 * 1024 * 1024) / 1024 / 1024));
          formInitialized.current = true;
        }
      }
    } catch (failure) { if (live.current && !controller.signal.aborted) setError(jobRecoveryError(failure)); }
    finally { if (live.current && !controller.signal.aborted) setLoading(false); }
  }, [caseId, selected]);
  useEffect(() => {
    if (onRunNavigate) return;
    try { const value = sessionStorage.getItem(viewKey); if (value && /^[0-9a-f-]{36}$/.test(value)) setSelected(value); }
    catch { /* The original flow remains accessible without saved navigation. */ }
  }, [viewKey, onRunNavigate]);
  useEffect(() => {
    formInitialized.current = false; lastObservation.current = '';
    live.current = true; setContext(null); setError(''); setNotice(''); setDraft(null); setPending(null); setCanRevise(false); setPendingProblem('');
    try { setPending(readPendingWorkflowRecovery(storageKey, selected, caseId)); }
    catch { setPendingProblem('本机确认记录无法核验。请保留记录，先查看关联流程。'); }
    void refresh();
    return () => { live.current = false; reading.current?.abort(); acting.current?.abort(); };
  }, [storageKey, selected, caseId, refresh]);

  const observation = `${observedRevision ?? ''}:${observedRunVersion ?? ''}:${observedEventSequence ?? ''}`;
  const contextStale = !!context && ((observedRevision !== undefined && observedRevision > context.current_revision)
    || (observedRunVersion !== undefined && observedRunVersion > context.run.version));
  useEffect(() => {
    if (!active || !context || busy || lastObservation.current === observation) return;
    const previous = lastObservation.current;
    lastObservation.current = observation;
    if (contextStale || previous !== '') void refresh();
  }, [active, context, busy, observation, contextStale, refresh]);
  const draftStale = !!draft && !!context && (draft.expected_revision !== context.current_revision
    || draft.expected_version !== context.run.version);

  function navigate(id: string) {
    if (onRunNavigate) { onRunNavigate(id); return; }
    try { sessionStorage.setItem(viewKey, id); } catch { /* Navigation is still available. */ }
    setSelected(id);
  }
  function accepted(run: RecoveryRun, confirmation: PendingWorkflowRecovery) {
    const relation = run.recomputation ?? run.legacy_migration, reset = run.recomputation_reset ?? run.legacy_migration_reset;
    if (run.id === confirmation.runId || run.case_id !== caseId || relation?.source_run_id !== confirmation.runId
      || relation.source_run_version !== confirmation.body.expected_version || reset?.confirmed_by !== userId
      || reset.from_revision !== confirmation.body.expected_revision || reset.to_state !== 'A') throw new Error('新旧流程回执无法核验');
    try { sessionStorage.removeItem(storageKey); setPending(null); } catch { /* Keep the original key for receipt replay. */ }
    setNotice(`已创建关联流程 ${run.id}；原流程和原请求保留。`); onChanged(); navigate(run.id);
  }
  function prepare() {
    if (!context?.action || readOnly || contextStale || mutation.current) return;
    const seconds = Number(timeout), bytes = Number(logMb) * 1024 * 1024, count = Number(subdomains);
    if (!runDir.trim() || !Number.isFinite(seconds) || seconds <= 0 || seconds > 86400 || !Number.isSafeInteger(bytes) || bytes < 1024 || bytes > 64 * 1024 * 1024) {
      setError('请核对计算目录、期限（大于0且不超过86400秒）和日志上限（1～64 MiB）。'); return;
    }
    const parameters: Record<string, unknown> = { geometry_mode: geometry, report_mode: report, solve_mode: solve };
    if (solve === 'mpi') {
      if (!Number.isSafeInteger(count) || count < 1 || count > 64) { setError('MPI分区数应为1～64的整数。'); return; }
      parameters.subdomains = count;
    }
    if (solve === 'udf') {
      if (!/^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(udf)) { setError('请填写以字母开头、最长40位的UDF名称，仅含字母、数字和下划线。'); return; }
      parameters.udf_name = udf;
    }
    const body: WorkflowRecoveryBody = { expected_version: context.run.version, expected_revision: context.current_revision,
      confirmed_reset: true, target, run_dir: runDir.trim(), parameters, budget: { timeout_seconds: seconds, max_log_bytes: bytes } };
    if (context.action === 'migrate') {
      body.expected_event_sequence = context.migration?.current_event_sequence;
      body.expected_source_plan_sha256 = context.migration?.source_plan_sha256;
    }
    setError(''); setDraft(body);
  }
  async function submit(existing?: PendingWorkflowRecovery) {
    if (readOnly || mutation.current || (!existing && (!draft || !context?.action || contextStale || draftStale))) return;
    let confirmation = existing;
    if (!confirmation) {
      confirmation = { schema: 1, runId: selected, caseId, key: crypto.randomUUID(), action: context!.action!, body: draft! };
      try { sessionStorage.setItem(storageKey, JSON.stringify(confirmation)); readPendingWorkflowRecovery(storageKey, selected, caseId); }
      catch { setError('无法保存本次确认，尚未发送请求。请允许浏览器会话存储。'); return; }
      setPending(confirmation); setDraft(null);
    }
    mutation.current = true; setBusy(true); setError(''); setCanRevise(false);
    const controller = new AbortController(); acting.current = controller;
    try {
      const run = await jobRecoveryRequest<RecoveryRun>(`/workflow-runs/${confirmation.runId}:${confirmation.action}`,
        { body: confirmation.body, key: confirmation.key, signal: controller.signal });
      if (live.current) accepted(run, confirmation);
    } catch (failure) {
      if (!live.current || controller.signal.aborted) return;
      setError(jobRecoveryError(failure));
      if (failure instanceof ApiError && [400, 409, 422].includes(failure.status)) {
        try {
          const result = await jobRecoveryRequest<{ found: boolean; run: RecoveryRun | null }>(`/workflow-runs/${confirmation.runId}/recovery-request`,
            { body: { key: confirmation.key, action: confirmation.action, payload: confirmation.body }, signal: controller.signal });
          if (live.current && !controller.signal.aborted) {
            if (result.found && result.run) accepted(result.run, confirmation);
            else if (!result.found && result.run === null) setCanRevise(true);
          }
        } catch { /* An unresolved receipt keeps its original confirmation. */ }
      }
      await refresh();
    } finally { mutation.current = false; if (live.current) { setBusy(false); feedback.current?.focus(); } }
  }
  async function control(action: 'cancel' | 'dispatch') {
    if (!context || readOnly || mutation.current) return;
    mutation.current = true; setBusy(true); setError('');
    const controller = new AbortController(); acting.current = controller;
    try {
      await jobRecoveryRequest(`/workflow-runs/${selected}:${action}`, { body: { expected_version: context.run.version }, signal: controller.signal });
      if (live.current) { setNotice(action === 'cancel' ? '已提交原流程取消，请刷新核对实际结束状态。' : '已核对并继续派发当前流程。'); onChanged(); await refresh(); }
    } catch (failure) { if (live.current && !controller.signal.aborted) { setError(jobRecoveryError(failure)); await refresh(); } }
    finally { mutation.current = false; if (live.current) { setBusy(false); feedback.current?.focus(); } }
  }
  function revise() {
    if (!canRevise || readOnly) return;
    try { sessionStorage.removeItem(storageKey); setPending(null); setCanRevise(false); setDraft(null); setError(''); void refresh(); }
    catch { setPendingProblem('未能清除已拒绝的确认，请保留记录。'); }
  }
  const blocked = disabled || busy || loading;
  const writeBlocked = blocked || readOnly;
  const terminal = !!context && ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(context.run.status);
  return <section className="input-recovery workflow-input-recovery" aria-label="工作流输入恢复" aria-busy={loading || busy}>
    <h4>在原流程中核对输入</h4>
    <p>重新计算会保留原流程及作业，以当前输入建立关联流程，并明确回到A阶段。后续B/C/D/E阶段仍需分别确认。</p>
    <div className="job-recovery-actions">
      {selected !== runId && <button disabled={blocked} onClick={() => navigate(runId)}>返回原请求所属流程</button>}
      <button disabled={blocked} onClick={() => { setDraft(null); setError(''); void refresh(); }}>刷新流程和当前输入</button>
    </div>
    {loading && <p role="status">正在核对流程和恢复记录…</p>}
    {context && <>
      <p>当前流程 {context.run.id} · {workflowTargetLabel(context.run.target)} · {workflowStatusLabel(context.run.status)}。当前输入版本 {context.current_revision}，阶段 {context.current_stage}。</p>
      {context.run.detail && <p>{context.run.detail}</p>}
      {context.source && <p>来源流程 <button className="job-recovery-link" disabled={blocked} onClick={() => navigate(context.source!.id)}>{context.source.id}</button>（{workflowStatusLabel(context.source.status)}）。</p>}
      {context.successors.items.map(run => <p key={run.id}>关联新流程 <button className="job-recovery-link" disabled={blocked} onClick={() => navigate(run.id)}>{run.id}</button> · {workflowStatusLabel(run.status)}。<span className="job-recovery-muted">审计 {run.audit_event_id}</span></p>)}
      {context.successors.next_before_seq && <button disabled={blocked} onClick={() => void refresh(context.successors.next_before_seq!)}>读取更早的关联流程</button>}
      <ol className="workflow-recovery-steps">{context.run.steps.map(step => <li key={step.id}>
        <span>{step.confirmation_state ? `确认${step.confirmation_state}阶段` : workflowTargetLabel(step.key)} · {workflowStatusLabel(step.status)}{step.execution_mode === 'reuse' ? step.status==='succeeded'?' · 已完成核验复用':' · 计划复用，尚未完成' : ''}</span>
        {step.attempts.map(attempt => <button key={attempt.id} className="job-recovery-link" disabled={blocked} onClick={() => onNavigate(attempt.job_id)}>作业 #{attempt.job_id}</button>)}
        {step.detail && <p>{step.detail}</p>}
      </li>)}</ol>
      {!terminal && <div className="job-recovery-actions">
        <button disabled={writeBlocked} onClick={() => void control('dispatch')}>核对并继续当前流程</button>
        <button disabled={writeBlocked || context.run.status === 'cancelling'} onClick={() => void control('cancel')}>取消当前流程</button>
      </div>}
      {context.run.steps.some(step => step.status === 'awaiting_confirmation') && <p>请在算例阶段确认入口核对产物并确认；此处继续流程不会自动确认阶段。</p>}
      {context.reason && <p>{context.reason.message}</p>}
      {context.reason && ['recovery_required','evidence_invalid'].includes(context.reason.code) && <div>
        <p>{context.run.steps.some(step=>step.attempts.length)?'请点击上方对应的“作业 #编号”，在作业面板展开“恢复与重新执行”。':'请在作业面板查看原流程与作业的保留记录，展开“恢复与重新执行”。'}由原主机负责人核查执行归属、进程停止和写入排空证明；尚无法核验时请保留现场，不能继续重算。</p>
        <p>需要核对保留文件时，使用项目面板的“文件版本与人工恢复”。登记保留、隔离版本或恢复文件不会补齐缺失的执行归属、停止或排空证明，也不会授权重新计算。</p>
      </div>}
    </>}
    {readOnly && <p role="status">当前工作区仅有读取权限。可查看原流程、作业和保留结果；创建新流程或改变阶段需要写权限。</p>}
    {contextStale && <p role="status">输入版本或原流程已变化，正在重新核对；已编辑的参数和原请求保留。读取失败时请刷新流程和当前输入。</p>}
    {pendingProblem && <p role="alert">{pendingProblem}</p>}
    {pending ? <div className="job-recovery-confirm">
      <p>上次确认的结果尚待核对。继续使用原确认，已创建则读取原关联流程。</p>
      <button disabled={writeBlocked} onClick={() => void submit(pending)}>继续核对此次工作流请求</button>
      {canRevise && <button disabled={writeBlocked} onClick={revise}>修改已拒绝且未创建的确认</button>}
    </div> : draft ? <div className="job-recovery-confirm">
      {draftStale && <p role="status">此确认绑定的输入版本或流程已变化。原确认未发送，请返回修改并重新核对。</p>}
      <p>确认以当前输入版本 {draft.expected_revision} 创建新流程，目标为{workflowTargetLabel(draft.target)}，计算目录为 {draft.run_dir}。原流程及作业保留。</p>
      <p>确认将工程阶段退回A；可核验的连续前置产物可能复用，其余重新计算。旧计划迁移会全部重建。具体复用和计算步骤将记录在新流程中。</p>
      {dependency && <RecomputeObservation graph={dependency} body={draft} />}
      <p>期限 {draft.budget.timeout_seconds} 秒，日志上限 {draft.budget.max_log_bytes / 1024 / 1024} MiB。</p>
      <p>几何处理：{draft.parameters.geometry_mode === 'export' ? '导出' : '预览'}；求解：{draft.parameters.solve_mode === 'mpi' ? `MPI并行（${draft.parameters.subdomains}个分区）` : draft.parameters.solve_mode === 'udf' ? `使用UDF ${draft.parameters.udf_name}` : '串行'}；报告：{draft.parameters.report_mode === 'markdown' ? 'Markdown报告' : '组合报告'}。</p>
      <button disabled={writeBlocked || contextStale || draftStale} onClick={() => void submit()}>确认回到A并创建关联流程</button>
      <button disabled={blocked} onClick={() => setDraft(null)}>返回修改</button>
    </div> : context?.action && !pendingProblem && !readOnly && <div className="input-recovery-fields">
      <label>新流程目标<select value={target} disabled={blocked} onChange={e => setTarget(e.target.value)}>{['geometry', 'mesh', 'solve', 'postprocess', 'report'].map(value => <option key={value} value={value}>{workflowTargetLabel(value)}</option>)}</select></label>
      <label>新流程计算目录<input value={runDir} maxLength={500} disabled={blocked} onChange={e => setRunDir(e.target.value)} /></label>
      <label>几何处理<select value={geometry} disabled={blocked} onChange={e => setGeometry(e.target.value)}><option value="preview">预览</option><option value="export">导出</option></select></label>
      <label>求解方式<select value={solve} disabled={blocked} onChange={e => setSolve(e.target.value)}><option value="serial">串行</option><option value="mpi">MPI并行</option><option value="udf">使用UDF</option></select></label>
      {solve === 'mpi' && <label>MPI分区数<input type="number" min="1" max="64" value={subdomains} disabled={blocked} onChange={e => setSubdomains(e.target.value)} /></label>}
      {solve === 'udf' && <label>UDF名称<input value={udf} maxLength={40} disabled={blocked} onChange={e => setUdf(e.target.value)} /></label>}
      <label>报告方式<select value={report} disabled={blocked} onChange={e => setReport(e.target.value)}><option value="compose">组合报告</option><option value="markdown">Markdown报告</option></select></label>
      <label>新流程期限（秒）<input type="number" min="1" max="86400" value={timeout} disabled={blocked} onChange={e => setTimeout(e.target.value)} /></label>
      <label>新流程日志上限（MiB）<input type="number" min="1" max="64" step="any" value={logMb} disabled={blocked} onChange={e => setLogMb(e.target.value)} /></label>
      <button disabled={blocked || contextStale} onClick={prepare}>核对当前输入与新流程</button>
    </div>}
    <p className="job-recovery-feedback" ref={feedback} tabIndex={-1} role={error ? 'alert' : 'status'}>{busy ? '正在核对工作流请求…' : error || notice}</p>
  </section>;
}

function RecomputeObservation({graph,body}:{graph:DependencySnapshot;body:WorkflowRecoveryBody}) {
  if(graph.dict_revision!==body.expected_revision||graph.run_dir!==body.run_dir)return <p>当前产物检查与拟提交的输入修订或目录不同，请在主面板刷新证据后再核对范围。</p>;
  const kinds=CORE_KINDS.slice(0,CORE_KINDS.indexOf(body.target)+1);
  const channels=graph.channels.filter(channel=>kinds.includes(channel.kind));
  const names=(statuses:string[])=>channels.filter(channel=>statuses.includes(channel.status)).map(channel=>dependencyLabel(channel.kind)).join('、')||'无';
  return <div><p>目标以内当前有效的产物：{names(['fresh'])}；需要生成或更新：{names(['missing','stale'])}；需先核查：{names(['unverifiable'])}。</p>
    <p>服务端会按本次目标、运行目录和参数再次核验；当前有效不代表本次必然复用。实际复用和重新计算范围会显示在关联新流程中。</p></div>;
}
