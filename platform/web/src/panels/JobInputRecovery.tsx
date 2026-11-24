import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { inputRecoveryKey, readPendingInputRecovery } from '../utils/inputRecovery';
import type { InputRecoveryBody, InputRecoveryPlan, InputRecoveryReceipt, PendingInputRecovery } from '../utils/inputRecovery';
import WorkflowInputRecovery from './WorkflowInputRecovery';

export default function JobInputRecovery({ jobId, caseId, userId, disabled, onBusy, onChanged, onNavigate }: {
  jobId: number; caseId: number; userId: number; disabled: boolean; onBusy: (value: boolean) => void;
  onChanged: () => void; onNavigate: (id: number) => void;
}) {
  const [plan, setPlan] = useState<InputRecoveryPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState<PendingInputRecovery | null>(null);
  const [pendingProblem, setPendingProblem] = useState('');
  const [canRevise, setCanRevise] = useState(false);
  const [draft, setDraft] = useState<InputRecoveryBody | null>(null);
  const [handler, setHandler] = useState('');
  const [parameters, setParameters] = useState('{}');
  const [command, setCommand] = useState('');
  const [runtime, setRuntime] = useState<InputRecoveryBody['runtime']>('windows');
  const [paths, setPaths] = useState('');
  const [timeout, setTimeout] = useState('300');
  const [logMb, setLogMb] = useState('16');
  const [note, setNote] = useState('');
  const live = useRef(true);
  const reading = useRef<AbortController | null>(null);
  const acting = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const feedback = useRef<HTMLParagraphElement>(null);
  const storageKey = inputRecoveryKey(userId, caseId, jobId);
  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);
  const refresh = useCallback(async () => {
    reading.current?.abort(); const controller = new AbortController(); reading.current = controller;
    setLoading(true);
    try {
      const value = await jobRecoveryRequest<InputRecoveryPlan>(`/jobs/${jobId}/input-recovery`, { signal: controller.signal });
      if (value.job_id !== jobId || value.case_id !== caseId) throw new Error('输入恢复归属无法核验');
      if (live.current && !controller.signal.aborted) setPlan(value);
    } catch (failure) { if (live.current && !controller.signal.aborted) setError(jobRecoveryError(failure)); }
    finally { if (live.current && !controller.signal.aborted) setLoading(false); }
  }, [jobId, caseId]);
  useEffect(() => {
    live.current = true;
    try { setPending(readPendingInputRecovery(storageKey, jobId)); }
    catch { setPendingProblem('本机的上次确认记录无法核验。请保留记录，先核对下方关联作业，暂不创建新的确认。'); }
    void refresh();
    return () => { live.current = false; reading.current?.abort(); acting.current?.abort(); };
  }, [storageKey, jobId, refresh]);

  function prepare() {
    if (!plan?.available || !plan.source_attempt_id || mutation.current) return;
    setError(''); setNotice('');
    const seconds = Number(timeout), bytes = Number(logMb) * 1024 * 1024;
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 86400 || !Number.isSafeInteger(bytes) || bytes < 1024 || bytes > 64 * 1024 * 1024) {
      setError('请填写大于 0 且不超过 86400 秒的期限，日志上限为 1～64 MiB。'); return;
    }
    const selected = plan.command_available ? null : plan.handler_choices.find(choice => `${choice.key}:${choice.version}` === handler);
    let value: Record<string, unknown> | null = null;
    if (!plan.command_available) {
      if (!selected) { setError('请明确选择用于新请求的处理器。'); return; }
      try { const parsed: unknown = JSON.parse(parameters); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(); value = parsed as Record<string, unknown>; }
      catch { setError('处理器参数必须是有效的 JSON 对象，例如 {}。'); return; }
    } else if (!command.trim()) { setError('请填写新请求需要执行的命令。旧命令不会自动沿用。'); return; }
    const inputPaths = paths.split('\n').map(path => path.trim()).filter(Boolean);
    if (inputPaths.length > 256) { setError('一次最多选择 256 个输入路径。'); return; }
    const next: InputRecoveryBody = { action: 'reprepare_current_inputs', source_attempt_id: plan.source_attempt_id,
      source_snapshot_sha256: plan.source_snapshot_sha256, expected_revision: plan.expected_revision,
      expected_event_sequence: plan.expected_event_sequence, handler: selected || null, parameters: value,
      command: plan.command_available ? command.trim() : null, runtime, input_paths: plan.command_available && inputPaths.length ? inputPaths : null,
      timeout_seconds: seconds, max_log_bytes: bytes, note };
    if (JSON.stringify(next).length > 68000) { setError('新请求内容过大，请缩小参数或输入路径范围。尚未发送请求。'); return; }
    setDraft(next);
  }

  async function submit(existing?: PendingInputRecovery) {
    if (mutation.current || (!existing && !draft)) return;
    let confirmed = existing;
    if (!confirmed) {
      confirmed = { schema: 1, jobId, key: crypto.randomUUID(), body: draft! };
      try {
        sessionStorage.setItem(storageKey, JSON.stringify(confirmed));
        readPendingInputRecovery(storageKey, jobId);
      } catch { setError('无法保存本次确认记录，尚未发送请求。请允许浏览器会话存储后重试。'); return; }
      setPending(confirmed); setDraft(null);
    }
    mutation.current = true; setBusy(true); setError(''); setNotice(''); setCanRevise(false);
    const controller = new AbortController(); acting.current = controller;
    try {
      const result = await jobRecoveryRequest<{ id: number; input_recovery: InputRecoveryReceipt }>(`/jobs/${jobId}:recover-inputs`,
        { body: confirmed.body, key: confirmed.key, signal: controller.signal });
      const receipt = result.input_recovery;
      if (!Number.isSafeInteger(result.id) || result.id <= 0 || receipt.target_job_id !== result.id || receipt.source_job_id !== jobId
        || receipt.case_id !== caseId || receipt.actor_user_id !== userId || receipt.old_execution_replayed !== false
        || receipt.original_inputs_reconstructed !== false || receipt.stop_evidence_created !== false || receipt.new_execution_requested !== true) throw new Error('新请求回执无法核验');
      if (!live.current) return;
      try { sessionStorage.removeItem(storageKey); setPending(null); } catch { /* Retain the exact key for safe replay. */ }
      setNotice(`已关联新作业 #${result.id}，原请求和原输入记录保留。`);
      await refresh(); onChanged(); onNavigate(result.id);
    } catch (failure) {
      if (live.current && !controller.signal.aborted) {
        setError(jobRecoveryError(failure));
        if (failure instanceof ApiError && ([400, 422].includes(failure.status)
          || ['input_stale', 'state_conflict', 'replacement_exists'].includes(failure.code ?? ''))) setCanRevise(true);
        await refresh();
      }
    } finally {
      mutation.current = false;
      if (live.current) { setBusy(false); feedback.current?.focus(); }
    }
  }

  function revise() {
    try { sessionStorage.removeItem(storageKey); setPending(null); setDraft(null); setCanRevise(false); setError(''); void refresh(); }
    catch { setPendingProblem('本机确认记录未能清除，请保留记录并稍后核对。'); }
  }
  const blocked = disabled || loading || busy;
  const recorded = !!plan?.links.length;
  if (plan?.workflow) return <WorkflowInputRecovery key={plan.workflow.run_id} runId={plan.workflow.run_id}
    caseId={caseId} userId={userId} disabled={disabled} onBusy={onBusy} onChanged={onChanged} onNavigate={onNavigate} />;
  return <section className="input-recovery" aria-label="输入核对与新请求" aria-busy={loading || busy}>
    <h4>{recorded ? '输入恢复记录' : '核对输入并创建独立新请求'}</h4>
    <p>{recorded ? '原请求与新作业各自保留输入和执行记录。此关联没有重建旧输入，也没有重放旧执行。'
      : '旧请求缺少可信输入封存。确认后会取消尚未执行的旧请求，以当前输入创建新作业；原记录保留。'}</p>
    {loading && <p role="status">正在核验原请求及可选操作…</p>}
    {plan?.links.map(link => <p key={link.id}>原作业 <button type="button" className="job-recovery-link" disabled={blocked} onClick={() => onNavigate(link.source_job_id)}>#{link.source_job_id}</button>
      已关联独立新作业 <button type="button" className="job-recovery-link" disabled={blocked} onClick={() => onNavigate(link.target_job_id)}>#{link.target_job_id}</button>。
      <span className="job-recovery-muted">审计 {link.id}；旧输入未重建，原执行未重放。</span></p>)}
    {plan?.reason && !plan.links.length && <p>{plan.reason.message}</p>}
    <button type="button" disabled={blocked} onClick={() => { setError(''); setDraft(null); void refresh(); }}>重新核对输入方案</button>
    {pendingProblem && <p role="alert">{pendingProblem}</p>}
    {pending ? <div className="job-recovery-confirm">
      <p>上次确认的结果尚待核对。继续时使用同一次确认，已创建则读取原新作业。</p>
      <button type="button" disabled={blocked} onClick={() => void submit(pending)}>继续核对此次新请求</button>
      {canRevise && <button type="button" disabled={blocked} onClick={revise}>修改被拒绝的确认</button>}
    </div> : draft ? <div className="job-recovery-confirm">
      <p>确认取消未执行的旧作业 #{jobId}，按当前输入版本 {draft.expected_revision} 创建并执行独立新作业。</p>
      <pre>{draft.handler ? `${draft.handler.key} · 版本 ${draft.handler.version}\n${JSON.stringify(draft.parameters, null, 2)}` : draft.command}</pre>
      <p>期限 {draft.timeout_seconds} 秒，日志上限 {draft.max_log_bytes / 1024 / 1024} MiB。</p>
      <button type="button" disabled={blocked} onClick={() => void submit()}>确认创建并执行新请求</button>
      <button type="button" disabled={blocked} onClick={() => setDraft(null)}>返回修改输入</button>
    </div> : plan?.available && !pendingProblem && <div className="input-recovery-fields">
      {plan.command_available ? <>
        <label>新请求命令<textarea rows={3} maxLength={4000} value={command} disabled={blocked} onChange={event => setCommand(event.target.value)} /></label>
        <label>新请求运行环境<select value={runtime} disabled={blocked} onChange={event => setRuntime(event.target.value as InputRecoveryBody['runtime'])}>
          <option value="windows">Windows</option><option value="wsl">WSL</option><option value="wsl-sandbox">隔离沙箱</option>
        </select></label>
        <label>当前输入路径（每行一个，可选）<textarea rows={3} value={paths} disabled={blocked} onChange={event => setPaths(event.target.value)} /></label>
        <p className="job-recovery-muted">未填写路径时，会封存当前算例的输入文件。新程序在独立工作副本中运行。</p>
      </> : <>
        <label>新请求处理器<select value={handler} disabled={blocked} onChange={event => setHandler(event.target.value)}>
          <option value="">请选择处理器</option>{plan.handler_choices.map(choice => <option key={`${choice.key}:${choice.version}`} value={`${choice.key}:${choice.version}`}>{choice.key} · 版本 {choice.version}</option>)}
        </select></label>
        <label>新处理器参数（JSON 对象）<textarea rows={5} value={parameters} disabled={blocked} onChange={event => setParameters(event.target.value)} /></label>
      </>}
      <label>新请求期限（秒）<input type="number" min="1" max="86400" value={timeout} disabled={blocked} onChange={event => setTimeout(event.target.value)} /></label>
      <label>新请求日志上限（MiB）<input type="number" min="1" max="64" value={logMb} disabled={blocked} onChange={event => setLogMb(event.target.value)} /></label>
      <label>输入恢复说明（可选）<textarea rows={2} maxLength={2000} value={note} disabled={blocked} onChange={event => setNote(event.target.value)} /></label>
      <button type="button" disabled={blocked} onClick={prepare}>核对新请求</button>
    </div>}
    <p className="job-recovery-feedback" ref={feedback} tabIndex={-1} role={error ? 'alert' : 'status'}>{busy ? '正在准备并提交新请求…' : error || notice}</p>
  </section>;
}
