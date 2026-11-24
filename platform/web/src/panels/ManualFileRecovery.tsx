import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import './ManualFileRecovery.css';

type FileEntry = { path: string; size: number; sha256: string };
type Plan = { source: string; snapshot_sha256: string; expected_revision: number; expected_event_sequence: number;
  files: FileEntry[]; special_entries: { path: string; kind: string }[]; available_actions: string[];
  blocked: { code: string; message: string } | null };
type RestorePlan = Pick<Plan, 'snapshot_sha256' | 'expected_revision' | 'expected_event_sequence' | 'blocked'> & { available: boolean };
type Receipt = { id: string; action: string; state: string; note: string; recorded_at: string;
  confirmation?: { request: Body; request_sha256: string; can_continue: boolean };
  result: { target_path?: string } | null; files?: FileEntry[]; special_entries?: { path: string; kind: string }[] };
type Body = { action: 'retain_for_review' | 'isolate_version' | 'restore_file'; source?: string; snapshot_sha256: string;
  expected_revision: number; expected_event_sequence: number; note: string; isolation_id?: string; file_path?: string;
  target_path?: string; expected_target_sha256?: string | null };
type Pending = { schema: 1; userId: number; caseId: number; body: Body } &
  ({ key: string; ticket?: never; requestSha256?: never } | { key?: never; ticket: string; requestSha256: string });
type HistoryPage = { items: Receipt[]; next_before_seq: number | null };
type Inventory = { backend?: string; status: string; preparations?: { status?: string; entries?: { cursor: string; state: string }[]; has_more?: boolean; next_cursor?: string | null } };
const labels: Record<string, string> = { retain_for_review: '登记保留', isolate_version: '隔离版本', restore_file: '恢复所选文件' };
const statusLabel: Record<string, string> = { completed: '已完成', pending: '待核对', committed_cleanup_pending: '已提交，待收尾', rejected: '已拒绝' };
const sourceLabel = (value: string) => value === 'pending' ? '当前待恢复事务'
  : value.startsWith('uploads/') ? `上传暂存现场 ${value.slice(8)}`
  : value.startsWith('objects/') ? `远端对象版本 ${value.slice(8)}` : `准备版本 ${value.replace('preparing-', '')}`;

function errorText(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return '登录已失效。重新登录后，请继续核对同一次确认。';
    if (error.status === 403 || error.status === 404) return '当前账户无法读取此版本，或原位置已变化。请刷新诊断和操作记录。';
    if (error.code === 'manual_adapter_unavailable') return '当前存储暂未接入人工版本操作。请保留远端版本，使用现有文件事务诊断。';
    if (['input_stale', 'file_conflict', 'revision_conflict'].includes(error.code ?? '')) return '输入或目标文件已经变化，本次确认未继续。请取消被拒绝的确认，重新检查版本。';
    return typeof (error.detail as { message?: unknown })?.message === 'string'
      ? (error.detail as { message: string }).message : error.message;
  }
  return '连接中断，操作可能已提交。请保留同一次确认，核对操作记录后继续。';
}

type SourceIntent={source:string;token:string}|null;
export default function ManualFileRecovery({ caseId,sourceIntent=null }: { caseId: number;sourceIntent?:SourceIntent }) {
  const userId = useStore(state => state.user?.id);
  return userId ? <Recovery key={`${userId}:${caseId}`} caseId={caseId} userId={userId} sourceIntent={sourceIntent} /> : null;
}

function Recovery({ caseId, userId,sourceIntent }: { caseId: number; userId: number;sourceIntent:SourceIntent }) {
  const [expanded, setExpanded] = useState(false), [loading, setLoading] = useState(false), [busy, setBusy] = useState(false);
  const [sources, setSources] = useState<string[]>([]), [source, setSource] = useState('pending'), [plan, setPlan] = useState<Plan | null>(null);
  const [history, setHistory] = useState<Receipt[]>([]), [selected, setSelected] = useState<Receipt | null>(null), [file, setFile] = useState('');
  const [nextHistory, setNextHistory] = useState<number | null>(null), [nextPreparation, setNextPreparation] = useState<string | null>(null);
  const [review, setReview] = useState<Receipt | null>(null);
  const [note, setNote] = useState(''), [target, setTarget] = useState(''), [preview, setPreview] = useState('');
  const [restore, setRestore] = useState<{ plan: RestorePlan; version: { exists: boolean; size: number; content_sha256: string | null } } | null>(null);
  const [pending, setPending] = useState<Pending | null>(null), [canRevise, setCanRevise] = useState(false);
  const [problem, setProblem] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const live = useRef(true), running = useRef(false), reading = useRef<AbortController | null>(null), acting = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const details=useRef<HTMLDetailsElement|null>(null),focusedSource=useRef<string|null>(null),handledIntent=useRef('');
  const base = `/cases/${caseId}`, url = `${base}/file-recovery`, storageKey = `vf-manual-file-recovery-v1:${userId}:${caseId}`;
  const current = () => live.current && useStore.getState().user?.id === userId && useStore.getState().currentCase?.id === caseId;

  useEffect(() => {
    live.current = true;
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (raw) {
        const value = JSON.parse(raw) as Pending, body = value.body;
        if (raw.length > 32768 || value.schema !== 1 || value.userId !== userId || value.caseId !== caseId
          || !(value.ticket ? /^[0-9a-f-]{36}$/.test(value.ticket) && /^[0-9a-f]{64}$/.test(value.requestSha256)
            && value.key === undefined : typeof value.key === 'string' && /^[0-9a-f-]{36}$/.test(value.key)) || !body || !labels[body.action]
          || !/^[0-9a-f]{64}$/.test(body.snapshot_sha256) || !Number.isSafeInteger(body.expected_revision)
          || body.expected_revision < 0 || !Number.isSafeInteger(body.expected_event_sequence) || body.expected_event_sequence < 0
          || typeof body.note !== 'string' || !body.note.trim()) throw new Error();
        setPending(value);
      }
    } catch { setProblem('本机确认记录无法核验。请保留记录，先查看服务端操作历史，暂不创建新的确认。'); }
    return () => { live.current = false; reading.current?.abort(); acting.current?.abort(); };
  }, [storageKey, caseId, userId]);

  async function refresh() {
    reading.current?.abort(); const controller = new AbortController(); reading.current = controller;
    const turn = ++generation.current; setLoading(true); setError(''); setPlan(null);
    try {
      const [inventory, rows] = await Promise.all([
        jobRecoveryRequest<Inventory>(`${base}/file-transactions?include_preparations=true&preparation_limit=20`, { signal: controller.signal }),
        jobRecoveryRequest<HistoryPage>(`${url}/actions-page?limit=20`, { signal: controller.signal }),
      ]);
      const choices = [...(inventory.status !== 'idle' ? ['pending'] : []), ...(inventory.preparations?.entries ?? []).map(row =>
        inventory.backend === 's3' ? `objects/${row.cursor.replace(/\/$/, '')}` : row.cursor)];
      if(focusedSource.current&&!choices.includes(focusedSource.current))choices.unshift(focusedSource.current);
      if (!current() || controller.signal.aborted || turn !== generation.current) return;
      setSources(choices); setHistory(rows.items); setNextHistory(rows.next_before_seq);
      setNextPreparation(inventory.preparations?.next_cursor ?? null);
      const chosen = focusedSource.current??(choices.includes(source) ? source : choices[0]);
      if (chosen) {
        setSource(chosen);
        const value = await jobRecoveryRequest<Plan>(`${url}?source=${encodeURIComponent(chosen)}`, { signal: controller.signal });
        if (current() && !controller.signal.aborted && turn === generation.current) setPlan(value);
      }
      if (inventory.preparations?.status === 'unavailable') setError('准备版本清单暂时不可读取；这不代表没有保留版本。');
    } catch (failure) { if (current() && !controller.signal.aborted && turn === generation.current) setError(errorText(failure)); }
    finally { if (current() && turn === generation.current) setLoading(false); }
  }
  async function loadMore(kind: 'history' | 'preparations') {
    const cursor = kind === 'history' ? nextHistory : nextPreparation;
    if (cursor === null || loading || busy) return;
    const turn = generation.current; setLoading(true); setError('');
    try {
      if (kind === 'history') {
        const page = await jobRecoveryRequest<HistoryPage>(`${url}/actions-page?limit=20&before_seq=${cursor}`);
        if (!current() || turn !== generation.current) return;
        setHistory(rows => [...rows, ...page.items.filter(row => !rows.some(old => old.id === row.id))]);
        setNextHistory(page.next_before_seq);
      } else {
        const page = await jobRecoveryRequest<Inventory>(`${base}/file-transactions?include_preparations=true&preparation_limit=20&preparation_after=${encodeURIComponent(cursor)}`);
        if (!current() || turn !== generation.current) return;
        if (page.preparations?.status === 'unavailable') throw new Error('准备版本清单暂时不可读取，请稍后继续加载。');
        const choices = (page.preparations?.entries ?? []).map(row => page.backend === 's3' ? `objects/${row.cursor.replace(/\/$/, '')}` : row.cursor);
        setSources(rows => [...new Set([...rows, ...choices])]); setNextPreparation(page.preparations?.next_cursor ?? null);
      }
    } catch (failure) { if (current() && turn === generation.current) setError(failure instanceof Error && !(failure instanceof ApiError) ? failure.message : errorText(failure)); }
    finally { if (current() && turn === generation.current) setLoading(false); }
  }

  async function reviewConfirmation(id: string) {
    setLoading(true); setError(''); setReview(null);
    try { const value = await jobRecoveryRequest<Receipt>(`${url}/actions/${id}`); if (current()) setReview(value); }
    catch (failure) { if (current()) setError(errorText(failure)); }
    finally { if (current()) setLoading(false); }
  }

  function adoptConfirmation() {
    if (!review?.confirmation?.can_continue || pending || !current()) return;
    const saved: Pending = { schema: 1, userId, caseId, ticket: review.id,
      requestSha256: review.confirmation.request_sha256, body: review.confirmation.request };
    try { sessionStorage.setItem(storageKey, JSON.stringify(saved)); setPending(saved); setReview(null); setNotice('已保存原确认。请选择“继续同一次确认”接续或核对原结果。'); }
    catch { setError('无法在本机保存原确认，尚未发送接续请求。'); }
  }
  useEffect(() => {
    if(sourceIntent&&sourceIntent.token!==handledIntent.current&&/^uploads\/[0-9a-f]{32}$/.test(sourceIntent.source)){
      handledIntent.current=sourceIntent.token;focusedSource.current=sourceIntent.source;setSource(sourceIntent.source);
      if(details.current){details.current.open=true;details.current.scrollIntoView({block:'nearest'});details.current.querySelector('summary')?.focus();}
      if(!expanded){setExpanded(true);return;}
    }
    if(expanded)void refresh();
  }, [expanded,sourceIntent?.token]);

  async function readSource(value: string) {
    setSource(value); setPlan(null); setError(''); setLoading(true);
    try { const result = await jobRecoveryRequest<Plan>(`${url}?source=${encodeURIComponent(value)}`); if (current()) setPlan(result); }
    catch (failure) { if (current()) setError(errorText(failure)); }
    finally { if (current()) setLoading(false); }
  }

  async function inspectIsolation(id: string) {
    setLoading(true); setError(''); setPreview(''); setRestore(null); setSelected(null); setFile('');
    try { const value = await jobRecoveryRequest<Receipt>(`${url}/actions/${id}`); if (current()) setSelected(value); }
    catch (failure) { if (current()) setError(errorText(failure)); }
    finally { if (current()) setLoading(false); }
  }

  async function inspectTarget() {
    if (!selected || !file || !target.trim()) return;
    setLoading(true); setError(''); setRestore(null);
    try {
      const [version, plan] = await Promise.all([
        jobRecoveryRequest<{ exists: boolean; size: number; content_sha256: string | null }>(`${base}/file:version?path=${encodeURIComponent(target.trim())}`),
        jobRecoveryRequest<RestorePlan>(`${url}/actions/${selected.id}/restore-plan?path=${encodeURIComponent(file)}`),
      ]);
      if (current()) setRestore({ version, plan });
    } catch (failure) { if (current()) setError(errorText(failure)); }
    finally { if (current()) setLoading(false); }
  }

  async function showPreview() {
    if (!selected || !file) return;
    setLoading(true); setError(''); setPreview('');
    try {
      const record = selected.files!.find(item => item.path === file)!;
      const token = localStorage.getItem('vf_token');
      const response = await fetch(`/api/v1${url}/actions/${selected.id}/file?path=${encodeURIComponent(file)}`, {
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(record.size ? { Range: `bytes=0-${Math.min(record.size, 16384) - 1}` } : {}) },
      });
      if (!response.ok) throw new ApiError(response.status, (await response.json()).detail);
      const text = await response.text(); if (current()) setPreview(text || '（空文件）');
    } catch (failure) { if (current()) setError(errorText(failure)); }
    finally { if (current()) setLoading(false); }
  }

  async function submit(action?: Body['action']) {
    if (running.current || !current()) return;
    let confirmed = pending;
    if (!confirmed) {
      if (!action || !note.trim()) { setError('请填写本次人工处理的原因。'); return; }
      let body: Body;
      if (action === 'restore_file') {
        if (!selected || !file || !restore?.plan.available) return;
        body = { action, isolation_id: selected.id, file_path: file, target_path: target.trim(), expected_target_sha256: restore.version.content_sha256,
          snapshot_sha256: restore.plan.snapshot_sha256, expected_revision: restore.plan.expected_revision,
          expected_event_sequence: restore.plan.expected_event_sequence, note: note.trim() };
      } else {
        if (!plan?.available_actions.includes(action)) return;
        body = { action, source, snapshot_sha256: plan.snapshot_sha256, expected_revision: plan.expected_revision,
          expected_event_sequence: plan.expected_event_sequence, note: note.trim() };
      }
      confirmed = { schema: 1, key: crypto.randomUUID(), userId, caseId, body };
      try {
        const raw = JSON.stringify(confirmed); if (raw.length > 32768) throw new Error();
        sessionStorage.setItem(storageKey, raw);
      } catch { setError('无法保存这次确认，尚未发送请求。请允许浏览器会话存储后重试。'); return; }
      setPending(confirmed);
    }
    running.current = true; setBusy(true); setError(''); setNotice(''); setCanRevise(false);
    const controller = new AbortController(); acting.current = controller;
    try {
      const result = confirmed.ticket
        ? await jobRecoveryRequest<Receipt>(`${url}/actions/${confirmed.ticket}:continue`, {
          body: { request_sha256: confirmed.requestSha256, confirmed: true }, signal: controller.signal })
        : await jobRecoveryRequest<Receipt>(`${url}/actions`, { body: confirmed.body, key: confirmed.key, signal: controller.signal });
      if (!current()) return;
      if (result.state === 'completed') {
        if(confirmed.body.action==='isolate_version'&&confirmed.body.source?.startsWith('uploads/'))focusedSource.current=null;
        sessionStorage.removeItem(storageKey); setPending(null); setRestore(null);
        setNotice(`${labels[confirmed.body.action]}已完成。原版本与操作记录保留；可重新检查当前文件。`);
        if (confirmed.body.action === 'isolate_version') await inspectIsolation(result.id);
        void useStore.getState().loadTree().catch(() => {});
      } else setNotice('已找到同一次确认，结果仍待核对。请保留原标识，稍后继续。');
      await refresh();
    } catch (failure) {
      if (current() && !controller.signal.aborted) {
        setError(errorText(failure));
        if (failure instanceof ApiError && ([400, 422].includes(failure.status)
          || ['input_stale', 'file_conflict', 'revision_conflict', 'validation_job_busy', 'validation_native_busy'].includes(failure.code ?? ''))) setCanRevise(true);
      }
    } finally { running.current = false; if (current()) setBusy(false); }
  }

  function revise() {
    try { sessionStorage.removeItem(storageKey); setPending(null); setCanRevise(false); setRestore(null); setError(''); void refresh(); }
    catch { setError('无法清除被拒绝的本机确认，请保留记录并稍后重试。'); }
  }
  const disabled = busy || loading || !!problem;
  return <details ref={details} className="manual-file-recovery" data-testid="manual-file-recovery" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>文件版本与人工恢复</summary>
    {expanded && <section aria-label="文件版本与人工恢复" aria-busy={busy || loading}>
      <p>核对保留版本，明确选择隔离或恢复文件。隔离不会判定原提交成功，也不会覆盖当前文件。</p>
      <button type="button" disabled={busy || loading} onClick={() => void refresh()}>刷新诊断与记录</button>
      {loading && <p role="status">正在读取保留版本…</p>}
      {problem && <p role="alert">{problem}</p>}
      {pending ? <div className="manual-file-confirm">
        <p>有一次“{labels[pending.body.action]}”尚待核对。继续会沿用原提交标识和确认内容。</p>
        <button type="button" disabled={disabled} onClick={() => void submit()}>继续同一次确认</button>
        {canRevise && <button type="button" disabled={busy || loading} onClick={revise}>取消被拒绝的确认</button>}
      </div> : <>
        {sources.length > 0 && <label>待处理版本<select value={source} disabled={disabled} onChange={event => void readSource(event.target.value)}>
          {sources.map(value => <option key={value} value={value}>{sourceLabel(value)}</option>)}
        </select></label>}
        {nextPreparation !== null && <button type="button" disabled={disabled} onClick={() => void loadMore('preparations')}>加载更多准备版本</button>}
        {plan && <>
          {plan.source.startsWith('uploads/')&&<p>这是上传暂存现场，完整性尚未确认。隔离后原上传入口关闭，保留内容继续占用存储。恢复其中的文件会作为一次新编辑，不代表原上传完成。</p>}
          <p>本版本保留 {plan.files.length} 个普通文件，原提交状态尚未判定。</p>
          {!!plan.special_entries.length && <p>另有 {plan.special_entries.length} 个链接或特殊条目，未跟随读取；不能作为普通文件恢复。</p>}
          {plan.blocked && <p>{plan.blocked.message}{plan.available_actions.includes('retain_for_review') ? '。可以先登记保留，再核对原写者或已有隔离记录。' : '。'}</p>}
          <label>处理原因<textarea value={note} maxLength={2000} rows={3} disabled={disabled} onChange={event => setNote(event.target.value)} /></label>
          <div className="manual-file-buttons">
            <button type="button" disabled={disabled || !note.trim() || !plan.available_actions.includes('retain_for_review')} onClick={() => void submit('retain_for_review')}>登记保留原版本</button>
            <button type="button" disabled={disabled || !note.trim() || !plan.available_actions.includes('isolate_version')} onClick={() => void submit('isolate_version')}>确认隔离此版本</button>
          </div>
        </>}
        {!loading && sources.length === 0 && !error && <p>未发现待处理事务。可以查看下方的隔离版本与操作记录。</p>}
        {selected && <div className="manual-file-selected">
          <h4>从隔离版本选择文件</h4>
          <p>{selected.note}</p>
          <label>保留文件<select value={file} disabled={disabled} onChange={event => { setFile(event.target.value); setRestore(null); setPreview(''); }}>
            <option value="">请选择文件</option>{selected.files?.map(item => <option key={item.path} value={item.path}>{item.path} · {item.size} B</option>)}
          </select></label>
          <button type="button" disabled={disabled || !file} onClick={() => void showPreview()}>预览所选文件</button>
          {preview && <><p>文本预览最多显示前16 KiB；原始保留文件未修改。</p><pre tabIndex={0}>{preview}</pre></>}
          <label>恢复到算例内路径<input value={target} maxLength={4096} disabled={disabled} onChange={event => { setTarget(event.target.value); setRestore(null); }} /></label>
          <label>恢复原因<textarea value={note} maxLength={2000} rows={3} disabled={disabled} onChange={event => setNote(event.target.value)} /></label>
          <button type="button" disabled={disabled || !file || !target.trim()} onClick={() => void inspectTarget()}>核对目标文件与恢复方案</button>
          {restore && <>
            <p>{restore.version.exists ? `当前目标存在，共 ${restore.version.size} B。确认后会以所选内容提交一次新编辑。` : '当前目标不存在。确认后会创建所选文件。'}隔离原件继续保留。</p>
            {restore.plan.blocked && <p>{restore.plan.blocked.message}</p>}
            <button type="button" disabled={disabled || !restore.plan.available || !note.trim()} onClick={() => void submit('restore_file')}>确认恢复所选文件</button>
          </>}
        </div>}
      </>}
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      {review?.confirmation && !pending && <div className="manual-file-confirm" aria-label="核对原确认">
        <h4>核对原确认</h4>
        <p>{labels[review.action] ?? '人工处理'} · {statusLabel[review.state] ?? '需核对'}</p>
        <p>{review.confirmation.request.action === 'restore_file'
          ? `恢复所选文件 ${review.confirmation.request.file_path} 到 ${review.confirmation.request.target_path}`
          : sourceLabel(review.confirmation.request.source ?? 'pending')}</p>
        <p>原确认原因：{review.confirmation.request.note}</p>
        <p>原确认依据：算例版本 {review.confirmation.request.expected_revision}，事件序号 {review.confirmation.request.expected_event_sequence}。接续仍核对原输入和目标，不自动采用当前版本。</p>
        {review.confirmation.can_continue ? <button type="button" disabled={disabled || review.state === 'rejected'} onClick={adoptConfirmation}>采用此原确认继续核对</button>
          : <p>此操作需由原确认账户在有写权限时接续。</p>}
        <button type="button" disabled={busy} onClick={() => setReview(null)}>关闭原确认</button>
      </div>}
      <h4>人工操作记录 · 已加载 {history.length} 条</h4>
      {!history.length && !loading && <p>尚无人工操作记录。</p>}
      <ul>{history.map(row => <li key={row.id}>
        <p><strong>{labels[row.action] ?? '人工处理'}</strong> · {statusLabel[row.state] ?? '需核对'}<br />{row.note}</p>
        <p className="manual-file-muted">{row.recorded_at}</p>
        <button type="button" disabled={disabled || !!pending} onClick={() => void reviewConfirmation(row.id)}>核对原确认</button>
        {row.action === 'isolate_version' && row.state === 'completed' && <button type="button" disabled={disabled || !!pending} onClick={() => void inspectIsolation(row.id)}>查看此隔离版本</button>}
      </li>)}</ul>
      {nextHistory !== null && <button type="button" disabled={disabled} onClick={() => void loadMore('history')}>加载更早操作记录</button>}
    </section>}
  </details>;
}
