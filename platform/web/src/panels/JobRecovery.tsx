import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../api";
import { jobRecoveryError, jobRecoveryReason, jobRecoveryRequest, pendingRepeatKey, readPendingRepeat, repeatBodyLabel, repeatLabel } from "../utils/jobRecovery";
import type { DispositionPlan, JobOperations, OperationOption, OutputPlan, PendingRepeat, PreparationPlan, PublicationPlan, RecoveryJob, RepeatAction, RepeatBody } from "../utils/jobRecovery";
import "./JobRecovery.css";
import JobInputRecovery from './JobInputRecovery';

export default function JobRecovery({ jobId, caseId, userId, revision, onChanged, onNavigate }: {
  jobId: number; caseId: number; userId: number; revision: string;
  onChanged: () => void; onNavigate: (jobId: number) => void;
}) {
  const headingId = useId();
  const [expanded, setExpanded] = useState(false);
  const [job, setJob] = useState<RecoveryJob | null>(null);
  const [operations, setOperations] = useState<JobOperations | null>(null);
  const [preparation, setPreparation] = useState<PreparationPlan | null>(null);
  const [output, setOutput] = useState<OutputPlan | null>(null);
  const [disposition, setDisposition] = useState<DispositionPlan | null>(null);
  const [publication, setPublication] = useState<PublicationPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState("");
  const [planErrors, setPlanErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [inputBusy, setInputBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [repeat, setRepeat] = useState<RepeatAction | null>(null);
  const [reprepare, setReprepare] = useState(false);
  const [reprepareHandler, setReprepareHandler] = useState('');
  const [parameters, setParameters] = useState("{}");
  const [time, setTime] = useState("");
  const [pending, setPending] = useState<PendingRepeat | null>(null);
  const [pendingProblem, setPendingProblem] = useState("");
  const [canRevise, setCanRevise] = useState(false);
  const live = useRef(true);
  const reading = useRef<AbortController | null>(null);
  const acting = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const status = useRef<HTMLParagraphElement>(null);
  const generation = useRef(0);
  const changed = useRef(onChanged);
  const navigate = useRef(onNavigate);
  useEffect(() => { changed.current = onChanged; navigate.current = onNavigate; }, [onChanged, onNavigate]);
  const storageKey = pendingRepeatKey(userId, caseId, jobId);

  useEffect(() => {
    live.current = true;
    try { setPending(readPendingRepeat(storageKey, jobId)); }
    catch { setPendingProblem("浏览器中的上次提交记录暂时无法核验。请保留记录，暂不创建新的重新执行请求。"); }
    return () => { live.current = false; ++generation.current; reading.current?.abort(); acting.current?.abort(); };
  }, [storageKey, jobId]);

  const refresh = useCallback(async () => {
    reading.current?.abort();
    const controller = new AbortController(); reading.current = controller;
    const selected = ++generation.current;
    setLoading(true); setReadError("");
    try {
      const current = await jobRecoveryRequest<RecoveryJob>(`/jobs/${jobId}`, { signal: controller.signal });
      if (current.id !== jobId) throw new Error("任务记录不匹配");
      const extras: string[] = [];
      const optional = async <T,>(path: string, name: string): Promise<T | null> => {
        try { return await jobRecoveryRequest<T>(path, { signal: controller.signal }); }
        catch (error) { if (!controller.signal.aborted) extras.push(name + "：" + jobRecoveryError(error)); return null; }
      };
      const unsettled = current.completion_pending || ["finalizing", "cancelling"].includes(current.phase ?? current.attempt?.phase ?? "");
      const outputs = current.attempt?.outputs;
      const [ops, manual, prep, archive, published] = await Promise.all([
        optional<JobOperations>(`/jobs/${jobId}/operations`, "重新执行方案"),
        optional<DispositionPlan>(`/jobs/${jobId}/recovery-disposition`, "人工核查方案"),
        unsettled && outputs?.finalizing_stage && !outputs.output_preparation
          ? optional<PreparationPlan>(`/jobs/${jobId}/preparation-recovery`, "证据接续方案") : Promise.resolve(null),
        outputs?.output_preparation && (unsettled || current.status === "cancelled")
          ? optional<OutputPlan>(`/jobs/${jobId}/output-recovery`, "输出接续方案") : Promise.resolve(null),
        optional<PublicationPlan>(`/jobs/${jobId}/publication-recovery`, "产物发布核对"),
      ]);
      if (!live.current || controller.signal.aborted || selected !== generation.current) return;
      setJob(current); setOperations(ops); setDisposition(manual); setPreparation(prep); setOutput(archive); setPublication(published); setPlanErrors(extras);
    } catch (error) {
      if (live.current && !controller.signal.aborted && selected === generation.current) setReadError(jobRecoveryError(error));
    } finally {
      if (live.current && selected === generation.current) setLoading(false);
    }
  }, [jobId]);
  useEffect(() => { if (expanded) void refresh(); }, [expanded, refresh, revision]);

  async function perform(label: string, operation: (signal: AbortSignal) => Promise<string>) {
    if (mutation.current) return;
    mutation.current = true;
    const controller = new AbortController(); acting.current = controller;
    setBusy(label); setNotice(""); setActionError("");
    try {
      const message = await operation(controller.signal);
      if (!live.current) return;
      setNotice(message); await refresh(); changed.current();
    } catch (error) {
      if (live.current && !controller.signal.aborted) { setActionError(jobRecoveryError(error)); await refresh(); }
    } finally {
      mutation.current = false;
      if (live.current) { setBusy(""); status.current?.focus(); }
    }
  }

  function recover(action: "preparation" | "archive" | "cache" | "finalize" | "retain" | "cancel") {
    const attempt = action === "preparation" ? preparation?.attempt_id : output?.attempt_id;
    const labels = { preparation: "接续证据与日志", archive: "补传原输出", cache: "核验执行缓存", finalize: "接续校验与发布", retain: "登记人工核查", cancel: "取消当前请求" };
    void perform(labels[action], async signal => {
      const path = action === "preparation" ? "recover-preparation" : action === "archive" ? "recover-outputs"
        : action === "cache" ? "restore-output-cache" : action === "finalize" ? "finalize-outputs"
          : action === "retain" ? "retain-for-review" : "cancel";
      const body = action === "preparation" ? { attempt_id: attempt, stage_sha256: preparation?.stage_sha256 }
        : action === "retain" ? { attempt_id: disposition?.attempt_id, snapshot_sha256: disposition?.snapshot_sha256, action: "retain_for_review" }
          : action === "cancel" ? {} : { attempt_id: attempt, inventory_sha256: output?.inventory_sha256 };
      const result = await jobRecoveryRequest<{ state?: string }>(`/jobs/${jobId}:${path}`, { signal, body });
      if (action === "retain") return "已登记人工核查。原文件和记录保留，原写入限制继续有效。";
      if (action === "cancel") return "取消请求已提交，仍需等待实际进程和读写结束。";
      return ({ captured: "原证据与日志已接续，请核对下一步。", archived: "原输出已归档，仍需完成校验与发布。", cache_ready: "原执行缓存已核验就绪。",
        succeeded: "原任务校验与发布已完成。", failed: "收尾已结束，原任务结果为失败。请查看原因与保留记录。", cancelled: "取消收尾已完成，待归档数据仍按记录保留。" } as Record<string, string>)[result.state ?? ""] ?? "操作已返回，请核对刷新后的任务记录。";
    });
  }

  function submitRepeat(existing?: PendingRepeat) {
    if (mutation.current) return;
    if (!existing && (!repeat || !operations?.source_attempt_id || operations.source_version === null)) return;
    let request = existing;
    if (!request) {
      try {
        const action = repeat!;
        const body: RepeatBody = { action, source_attempt_id: operations!.source_attempt_id!, source_version: operations!.source_version! };
        if (action === "rerun") {
          body.expected_revision = operations!.expected_revision; body.expected_event_sequence = operations!.expected_event_sequence;
          if (reprepare) {
            const selected = operations!.reprepare.handler_choices.find(item => `${item.key}:${item.version}` === reprepareHandler);
            if (!selected || !operations!.reprepare.available) { setActionError('请明确选择原作业类型对应的重新准备处理器。'); return; }
            body.reprepare_handler = selected;
          }
          if (reprepare || operations!.rerun.parameters_required) {
            const value: unknown = JSON.parse(parameters);
            if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("parameters");
            body.parameters = value as Record<string, unknown>;
          }
        }
        if (action === "resume") {
          if (!time.trim()) { setActionError("请填写原尝试中需要续算的检查点时刻。"); return; }
          body.parameters = { time: time.trim() };
        }
        request = { schema: 1, jobId, key: crypto.randomUUID(), body };
        const saved = JSON.stringify(request);
        if (saved.length > 70000) { setActionError('确认内容过大，请缩小处理器参数。尚未保存或发送请求。'); return; }
        sessionStorage.setItem(storageKey, saved);
      } catch (error) {
        setActionError(error instanceof SyntaxError || (error instanceof Error && error.message === "parameters")
          ? "处理器参数必须是有效的 JSON 对象，例如 {}。" : "无法保存本次确认记录，尚未发送请求。请允许浏览器会话存储后重试。");
        return;
      }
      setPending(request); setRepeat(null); setCanRevise(false);
    }
    const confirmed = request;
    setCanRevise(false);
    void perform(repeatBodyLabel(confirmed.body), async signal => {
      try {
        const result = await jobRecoveryRequest<{ id: number }>(`/jobs/${jobId}:repeat`, { signal, body: confirmed.body, key: confirmed.key });
        if (!Number.isSafeInteger(result.id) || result.id <= 0) throw new Error("任务回执无法核验");
        try { sessionStorage.removeItem(storageKey); if (live.current) setPending(null); } catch { /* Same key remains safe to confirm again. */ }
        if (live.current) navigate.current(result.id);
        return `已关联作业 #${result.id}，原作业 #${jobId} 和原尝试仍保留。`;
      } catch (error) {
        if (live.current && error instanceof ApiError && ([400, 422].includes(error.status)
          || ["state_conflict", "input_stale", "revision_conflict", "invalid_request"].includes(error.code ?? ""))) setCanRevise(true);
        throw error;
      }
    });
  }

  function reviseRejected() {
    try { sessionStorage.removeItem(storageKey); setPending(null); setCanRevise(false); setActionError(""); void refresh(); }
    catch { setPendingProblem("本机确认记录尚未清除，请保留记录并稍后重试。"); }
  }

  const disabled = !!busy || inputBusy || loading || !!readError;
  const files = Object.keys(job?.attempt?.outputs?.files ?? {});
  const retained = disposition?.disposition;
  const needsReview = job?.recovery?.state === "requires_attention" || !!job?.record_issues?.length
    || ["failed", "cancelled", "interrupted"].includes(job?.status ?? "");
  const canFinish = output?.finalization_pending && ["archived", "captured", "requires_attention"].includes(output.state);
  const phase = job?.phase ?? job?.attempt?.phase;
  const optionReason = (option: OperationOption) => operations?.reprepare.available && option.reason?.code === 'recovery_required'
    ? '旧请求未绑定正式处理器，请选择“用当前输入重新准备处理器”。'
    : option.reason ? jobRecoveryReason(option.reason.code) : undefined;

  return <details className="job-recovery" onToggle={event => setExpanded(event.currentTarget.open)} data-testid="job-recovery">
    <summary>恢复与重新执行</summary>
    {expanded && <section className="job-recovery-content" aria-labelledby={headingId} aria-busy={loading || !!busy}>
      <div className="job-recovery-heading"><h3 id={headingId}>作业 #{jobId} 的恢复方案</h3>
        <button type="button" disabled={disabled} onClick={() => void refresh()}>刷新方案</button></div>
      {loading && <p role="status">正在核验任务、原输入和可用操作…</p>}
      {readError && <p role="alert">{readError} <button type="button" disabled={!!busy || loading} onClick={() => void refresh()}>重新读取</button></p>}
      {job?.recovery && <p>{jobRecoveryReason(job.recovery.reason)}</p>}
      {!!job?.record_issues?.length && <ul aria-label="无法核验的作业记录">
        {job.record_issues.map(issue => <li key={issue.field}>{issue.message}</li>)}
      </ul>}
      {phase === "cancelling" && <p>取消已受理，实际停止或读写结束尚待核验。</p>}
      {phase === "finalizing" && <p>任务的收尾状态仍待核验。接续操作保留原任务，不重新运行原求解器。</p>}
      {job?.attempt && <p className="job-recovery-muted">当前为第 {job.attempt.number} 次尝试 · 根作业 #{job.attempt.root_job_id}</p>}
      {job?.attempt?.operation && <p>本次{({ retry: "重试", rerun: "重跑", resume: "续算" } as Record<string, string>)[job.attempt.operation.action] ?? "操作"}关联原作业
        <button type="button" className="job-recovery-link" disabled={!!busy} onClick={() => navigate.current(job.attempt!.operation!.source_job_id)}>#{job.attempt.operation.source_job_id}</button>。
        {job.attempt.operation.checkpoint_time && <>检查点时刻 {job.attempt.operation.checkpoint_time}。</>}</p>}
      {files.length > 0 && <details><summary>查看保留的输出清单（{files.length} 个文件）</summary>
        <ul className="job-recovery-files">{files.map(path => <li key={path}>{path}</li>)}</ul>
        <p className="job-recovery-muted">清单用于定位原副本，文件是否可恢复仍需完整核验。</p></details>}
      {planErrors.map(message => <p key={message} className="job-recovery-muted">{message}</p>)}
      {publication && <div aria-label="产物发布核对">
        <h4>产物发布核对</h4>
        <p>{({ no_publication: '没有关联的正式产物记录。', legacy_unjournaled: '原产物缺少可核验的发布日志，请保留原版本人工核查。',
          requires_attention: '原产物或发布日志无法完整核验，请保留原版本。', copy_incomplete: '原产物复制尚未完成，不能据此确认发布。',
          bundle_verified: '保留的产物版本与发布清单一致。', state_changed: '核对期间发布记录已变化，请刷新方案。' } as Record<string, string>)[publication.state] ?? '产物状态仍需核查。'}</p>
        {publication.database_status && <p>发布记录：{({ prepared: '已准备，尚未确认发布', committed: '已提交', superseded: '已有后续版本', failed: '发布失败' } as Record<string, string>)[publication.database_status] ?? '状态待核查'}。
          {publication.active === true ? '当前引用指向此版本。' : publication.active === false ? '当前引用未指向此版本。' : ''}</p>}
        {job?.completion_pending && <p>发布收尾仍待核验，尚不能确认作业完成。</p>}
        {job?.attempt?.outputs?.completion?.alias_error && <p>兼容路径更新尚未完成，请结合正式产物记录核查；原产物版本保留。</p>}
        {publication.next_step === 'retain_for_finalizer' && <p>此版本保留给原任务收尾；请按下方可用方案核验，不另起原生执行。</p>}
      </div>}
      {(preparation || output || needsReview) && <h4>接续原任务</h4>}
      <div className="job-recovery-actions">
        {preparation?.state === "preparation_pending" && <button type="button" disabled={disabled || preparation.owner_observation.state !== "exited"} onClick={() => recover("preparation")}>接续证据与日志</button>}
        {output?.state === "recoverable" && <button type="button" disabled={disabled} onClick={() => recover("archive")}>补传原输出</button>}
        {output && ["archived", "captured"].includes(output.state) && output.finalization_pending && <button type="button" disabled={disabled} onClick={() => recover("cache")}>核验执行缓存</button>}
        {canFinish && <button type="button" disabled={disabled} onClick={() => recover("finalize")}>{job?.status === "cancelled" ? "核验取消收尾" : "接续校验与发布"}</button>}
        {needsReview && disposition?.available_actions.includes("retain_for_review") && !retained && <button type="button" disabled={disabled} onClick={() => recover("retain")}>保留现场并登记人工核查</button>}
        {job && !job.completion_pending && ["queued", "running"].includes(job.status) && phase !== "cancelling" && <button type="button" disabled={disabled} onClick={() => recover("cancel")}>取消当前请求</button>}
      </div>
      {preparation?.owner_observation.state === "match" && <p>原执行者仍在运行，暂时不能接管其收尾。</p>}
      {preparation && !["match", "exited"].includes(preparation.owner_observation.state) && <p>无法确认原执行者已经退出，请保留现场核查。</p>}
      {output?.archive_pending && <p>待归档输出仍保留；未核验原写入结束前，不宣告归档或取消收尾完成。</p>}
      {retained && <p>已登记人工核查，原写入限制保持。<span className="job-recovery-muted">审计记录 {retained.event_id}</span></p>}
      {disposition?.issues.includes('write_checkpoint_unverified') && <p>原写入检查点无法核验。登记人工核查只保留说明，不能替代停止和写入结束证明。</p>}
      {needsReview && <p className="job-recovery-muted">请保留原文件、日志和进程记录。管理员需在原执行主机核验停止与读写；人工登记不会授权发布、删除或控制未知进程。</p>}
      {job && (!!job.run_id || job.recovery?.reason === 'input_recovery_required' || !!job.input_recoveries?.length
        || !!job.attempt && !job.attempt.workspace) && <JobInputRecovery key={`${userId}:${caseId}:${jobId}`}
          userId={userId} caseId={caseId} jobId={jobId} disabled={!!busy || loading || !!readError}
          onBusy={setInputBusy} onChanged={() => changed.current()} onNavigate={id => navigate.current(id)} />}
      <h4>创建新尝试</h4>
      <p className="job-recovery-muted">请按可用方案确认输入和参数。操作会创建新作业，原记录保留。</p>
      {pendingProblem && <p role="alert">{pendingProblem}</p>}
      {pending ? <div className="job-recovery-confirm">
        <p>有一次“{repeatBodyLabel(pending.body)}”尚待核对。继续时沿用同一个提交标识；已提交则读取原回执，未提交则按上次确认参数提交一次。</p>
        <button type="button" disabled={disabled || !!pendingProblem} onClick={() => submitRepeat(pending)}>继续同一次提交</button>
        {canRevise && <button type="button" disabled={disabled} onClick={reviseRejected}>取消被拒绝的确认并刷新方案</button>}
      </div> : <div className="job-recovery-actions">{(["retry", "rerun", "resume"] as const).map(action => <button type="button" key={action}
        disabled={disabled || !!pendingProblem || !operations?.[action].available} title={operations ? optionReason(operations[action]) : undefined}
        onClick={() => {
          if (action === "resume") setTime(operations?.resume.supports_latest ? "latest" : "");
          setRepeat(action); setReprepare(false); setActionError("");
        }}>{repeatLabel(action)}</button>)}
        {operations?.reprepare.available && <button type="button" disabled={disabled || !!pendingProblem}
          onClick={() => { setRepeat('rerun'); setReprepare(true); setActionError(''); }}>用当前输入重新准备处理器</button>}
      </div>}
      {!pending && operations?.retry.reason && !operations.retry.available && <p className="job-recovery-muted">重试条件：{optionReason(operations.retry)}</p>}
      {!pending && operations?.resume.reason?.code === "checkpoint_disabled" && <p className="job-recovery-muted">{optionReason(operations.resume)}</p>}
      {repeat && !pending && <form className="job-recovery-confirm" onSubmit={event => { event.preventDefault(); submitRepeat(); }}>
        <h4>确认{reprepare ? '用当前输入重新准备处理器' : repeatLabel(repeat)}</h4>
        <p>{reprepare ? '选择正式处理器，按当前输入和本次参数准备独立新作业。原命令、旧尝试和结果保留，旧命令不会重放。'
          : repeat === "retry" ? "使用原尝试封存的输入。不会替换原记录。" : repeat === "rerun" ? "使用当前算例输入和本次确认的参数。输入版本变化后需要重新确认。" : "使用原尝试的实际检查点。服务端会重新核验时刻、原场文件与停止证明。"}</p>
        {reprepare && <label>重新准备处理器<select value={reprepareHandler} disabled={disabled} required onChange={event => setReprepareHandler(event.target.value)}>
          <option value="">请选择处理器</option>{operations?.reprepare.handler_choices.map(item => <option key={`${item.key}:${item.version}`} value={`${item.key}:${item.version}`}>{item.key} · 版本 {item.version}</option>)}
        </select></label>}
        {repeat === "resume" && operations?.resume.supports_latest && <label>检查点选择
          <select value={time === "latest" ? "latest" : "explicit"} disabled={disabled}
            onChange={event => setTime(event.target.value === "latest" ? "latest" : "")}>
            <option value="latest">最近一次写出的时刻</option>
            <option value="explicit">指定时刻</option>
          </select></label>}
        {repeat === "resume" && (time !== "latest" || !operations?.resume.supports_latest) && <label>检查点时刻
          <input value={time} maxLength={128} required disabled={disabled} placeholder="例如 0.005" onChange={event => setTime(event.target.value)} />
        </label>}
        {repeat === "rerun" && (reprepare || operations?.rerun.parameters_required) && <label>处理器参数（JSON 对象）
          <textarea value={parameters} maxLength={65536} rows={5} spellCheck={false} onChange={event => setParameters(event.target.value)} aria-describedby={headingId + "-parameters-help"} />
          <span id={headingId + "-parameters-help"} className="job-recovery-muted">{reprepare ? '按选定处理器填写新参数。' : '按原专业处理器填写参数。'}服务端校验通过后才创建新任务。</span></label>}
        <div className="job-recovery-actions"><button type="submit" disabled={disabled}>确认并创建新尝试</button><button type="button" disabled={!!busy} onClick={() => setRepeat(null)}>返回方案</button></div>
      </form>}
      <p ref={status} tabIndex={-1} role={actionError ? "alert" : "status"} aria-live="polite" className="job-recovery-feedback">{busy ? `正在${busy}…` : actionError || notice}</p>
    </section>}
  </details>;
}
