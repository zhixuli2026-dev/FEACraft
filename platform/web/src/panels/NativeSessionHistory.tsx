import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { kindNames, nativeSessionClosedEvent, sessionHistoryError, sessionHistoryRequest, sessionIssue, sessionPhase } from '../utils/nativeSessionHistory';
import type { NativeKind, NativeSessionClosed, SessionHistoryPlan, SessionHistoryRow, SessionReview, SessionLink, SessionLinkPlan } from '../utils/nativeSessionHistory';
import './NativeSessionHistory.css';

interface Props {
  caseId?: number; userId: number; initialKind: NativeKind;
  onTerminalSelect?: (id: string) => void; onVizSelect?: (id: number) => void;
  onVizReopen?: (id: number) => void; vizOpening?: boolean;
  onConversationSelect?: (id: number, runtime: string) => Promise<void>;
}
export default function NativeSessionHistory(props: Props) {
  const heading = useId();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState(props.initialKind);
  const [scope, setScope] = useState(props.caseId ? 'case' : 'all');
  const [rows, setRows] = useState<SessionHistoryRow[]>([]);
  const [selected, setSelected] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const reading = useRef<AbortController | null>(null);
  const navigating = useRef<AbortController | null>(null);
  const load = useCallback(async (before?: string) => {
    reading.current?.abort(); const controller = new AbortController(); reading.current = controller;
    setBusy(true); setError('');
    const query = new URLSearchParams({ limit: '20' });
    if (scope === 'case' && props.caseId) query.set('case_id', String(props.caseId));
    if (before) query.set('cursor', before);
    try {
      const value = await sessionHistoryRequest<{ data: SessionHistoryRow[]; next_cursor: string | null }>(`/native-sessions/${kind}?${query}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setRows(previous => before ? [...previous, ...value.data.filter(row => !previous.some(old => old.id === row.id))] : value.data);
      setCursor(value.next_cursor);
      if (!before) setSelected(previous => value.data.some(row => row.id === previous) ? previous : value.data[0]?.id ?? '');
    } catch (failure) { if (!controller.signal.aborted) setError(sessionHistoryError(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }, [kind, scope, props.caseId]);
  useEffect(() => {
    setRows([]); setSelected(''); setCursor(null);
    if (open) void load();
    return () => { reading.current?.abort(); navigating.current?.abort(); };
  }, [open, load]);
  const updateRow = useCallback((value: SessionHistoryRow) => {
    setRows(previous => previous.map(row => row.kind === value.kind && row.id === value.id ? value : row));
  }, []);
  async function selectRelated(id: string) {
    navigating.current?.abort(); const controller = new AbortController(); navigating.current = controller;
    try {
      const value = await sessionHistoryRequest<SessionHistoryPlan>(`/native-sessions/${kind}/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (scope === 'case' && value.case_id !== props.caseId) { setError('关联记录属于其他算例，请先切换范围核对。'); return; }
      setRows(previous => previous.some(row => row.id === id) ? previous : [...previous, value]); setSelected(id);
    } catch (failure) { if (!controller.signal.aborted) setError(sessionHistoryError(failure)); }
  }
  const visible = rows.filter(row => row.kind === kind && (scope !== 'case' || row.case_id === props.caseId));
  return <details className="native-session-history" data-testid="native-session-history" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>会话历史与人工核查</summary>
    {open && <section aria-labelledby={heading} className="native-session-history-content">
      <h3 id={heading}>保留的原生会话</h3>
      <p>读取历史不会创建会话或发送模型任务。失联、关闭和停止已核验分别显示。</p>
      <div className="native-session-history-controls">
        <label>类型<select value={kind} disabled={busy} onChange={event => setKind(event.target.value as NativeKind)}>
          {(['terminal', 'cli', 'viz'] as const).map(value => <option key={value} value={value}>{kindNames[value]}</option>)}
        </select></label>
        <label>范围<select value={scope} disabled={busy} onChange={event => setScope(event.target.value)}>
          {props.caseId && <option value="case">当前算例</option>}<option value="all">我的全部会话</option>
        </select></label>
        <button type="button" disabled={busy} onClick={() => void load()}>{busy ? '正在读取…' : '刷新会话历史'}</button>
      </div>
      {error && <p role="alert">{error}</p>}
      {!busy && !error && !visible.length && <p role="status">此范围没有可读取的{kindNames[kind]}会话。可切换范围查看其他记录。</p>}
      {visible.length > 0 && <label className="native-session-history-select">选择会话记录<select aria-label="选择会话记录" value={selected} disabled={busy}
        onChange={event => setSelected(event.target.value)}>
        {visible.map(row => <option key={row.id} value={row.id}>{sessionPhase(row.phase)} · {row.runtime} · {row.id.slice(0, 12)}</option>)}
      </select></label>}
      {cursor && <button type="button" disabled={busy} onClick={() => void load(cursor)}>读取更早的会话</button>}
      {visible.some(row => row.id === selected) && <SessionDetails key={`${props.userId}:${kind}:${selected}`} kind={kind} id={selected}
        candidates={visible} onRelatedSelect={selectRelated} onRefresh={updateRow} {...props} />}
    </section>}
  </details>;
}

function SessionDetails({ kind, id, caseId, userId, onTerminalSelect, onVizSelect, onVizReopen, vizOpening, onConversationSelect, candidates, onRelatedSelect, onRefresh }: Props & {
  kind: NativeKind; id: string; candidates: SessionHistoryRow[]; onRelatedSelect: (id: string) => Promise<void>;
  onRefresh: (row: SessionHistoryRow) => void;
}) {
  const heading = useId();
  const [plan, setPlan] = useState<SessionHistoryPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [actionError, setActionError] = useState('');
  const [tail, setTail] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState('');
  const [linkBusy, setLinkBusy] = useState(false);
  const mounted = useRef(true);
  const reading = useRef<AbortController | null>(null);
  const acting = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const feedback = useRef<HTMLParagraphElement>(null);
  const prefix = `/native-sessions/${kind}/${encodeURIComponent(id)}`;
  const refresh = useCallback(async () => {
    reading.current?.abort(); const controller = new AbortController(); reading.current = controller;
    setLoading(true); setError('');
    try {
      const value = await sessionHistoryRequest<SessionHistoryPlan>(prefix, { signal: controller.signal });
      if (!controller.signal.aborted && mounted.current) { setPlan(value); onRefresh(value); }
    } catch (failure) { if (!controller.signal.aborted && mounted.current) setError(sessionHistoryError(failure)); }
    finally { if (!controller.signal.aborted && mounted.current) setLoading(false); }
  }, [prefix, onRefresh]);
  useEffect(() => {
    mounted.current = true; void refresh();
    return () => { mounted.current = false; reading.current?.abort(); acting.current?.abort(); };
  }, [refresh]);
  async function act(action: 'retain_for_review' | 'reconcile_scope' | 'reconcile_initialization') {
    if (mutation.current || !plan?.available_actions.includes(action)) return;
    mutation.current = true; const controller = new AbortController(); acting.current = controller;
    setBusy(action); setActionError(''); setNotice('');
    try {
      if (action === 'retain_for_review') {
        const result = await sessionHistoryRequest<SessionReview>(prefix + ':retain-for-review', { signal: controller.signal,
          body: { action, snapshot_sha256: plan.snapshot_sha256, note } });
        if (mounted.current) setNotice(`已登记人工核查，凭证 ${result.id}。原停止和写入限制保持。`);
      } else {
        await sessionHistoryRequest(`/codex/sessions/${encodeURIComponent(id)}:${action === 'reconcile_scope' ? 'reconcile-scope' : 'reconcile-initialization'}`,
          { signal: controller.signal, body: {} });
        if (mounted.current) setNotice(action === 'reconcile_scope' ? '范围收尾核验已返回；会话执行锁和暂存文件仍需独立核验。' : '初始化占用核验已返回；这不是会话完整停止证明。');
      }
      if (mounted.current) await refresh();
    } catch (failure) { if (mounted.current && !controller.signal.aborted) { setActionError(sessionHistoryError(failure)); await refresh(); } }
    finally { mutation.current = false; if (mounted.current) { setBusy(''); feedback.current?.focus(); } }
  }
  async function readTail() {
    if (mutation.current) return;
    mutation.current = true; const controller = new AbortController(); acting.current = controller;
    setBusy('read_tail'); setActionError(''); setNotice('');
    try {
      const value = await sessionHistoryRequest<{ tail: string | null }>(`/terminal/sessions/${encodeURIComponent(id)}`, { signal: controller.signal });
      if (mounted.current) {
        if (typeof value.tail === 'string') { setTail(value.tail); setNotice('已读取原日志，未重新连接或启动终端。'); }
        else setActionError('原日志尚未通过记录核验，请保留现有文件。');
      }
    } catch (failure) { if (mounted.current && !controller.signal.aborted) setActionError(sessionHistoryError(failure)); }
    finally { mutation.current = false; if (mounted.current) { setBusy(''); feedback.current?.focus(); } }
  }
  const canClose = !!plan && (kind === 'terminal' || kind === 'viz') && plan.phase === 'active'
    && plan.owner_available && plan.alive === true;
  async function closeSelectedSession() {
    if (!canClose || !plan || mutation.current) return;
    const endpoint = kind === 'terminal' ? '/terminal/sessions/' : '/viz:sessions/';
    const session: NativeSessionClosed = { kind, id, case_id: plan.case_id, userId };
    mutation.current = true;
    setBusy('close_session'); setActionError(''); setNotice('');
    try {
      const result = await sessionHistoryRequest<{ ok: boolean }>(endpoint + encodeURIComponent(id), { method: 'DELETE' });
      if (!result.ok) throw new Error('Session stop was not confirmed');
      window.dispatchEvent(new CustomEvent<NativeSessionClosed>(nativeSessionClosedEvent, { detail: session }));
      if (mounted.current) { setNotice('已关闭此会话，停止已核验。'); await refresh(); }
    } catch (failure) {
      if (mounted.current) { setNotice(''); setActionError(sessionHistoryError(failure)); await refresh(); }
    } finally {
      mutation.current = false;
      if (mounted.current) { setBusy(''); feedback.current?.focus(); }
    }
  }
  const disabled = !!busy || linkBusy || loading || !!error;
  const sameCase = plan?.case_id === (caseId ?? null);
  return <section className="native-session-history-detail" aria-labelledby={heading} aria-busy={loading || !!busy}>
    <div className="native-session-history-controls"><h4 id={heading}>{kindNames[kind]}会话详情</h4>
      <button type="button" disabled={loading || !!busy} onClick={() => void refresh()}>刷新核查方案</button></div>
    {loading && <p role="status">正在读取原记录和核查凭证…</p>}
    {plan && <>
      <p className="native-session-history-id">会话 {id}</p>
      <p>{sessionPhase(plan.phase)} · {plan.case_id === null ? '未绑定算例' : `算例 #${plan.case_id}`} · 原记录版本 {plan.version}</p>
      <p>{plan.owner_available ? '原执行者可由当前服务连接。' : '当前服务未连接原执行者。'}
        {plan.cleanup_verified === true ? ' 完整停止已核验。' : ' 完整停止尚未核验。'}</p>
      {plan.issues.map(issue => <p key={issue}>{sessionIssue(issue)}</p>)}
      {plan.error && <p className="native-session-history-muted">原记录分类：{plan.error}</p>}
      <div className="native-session-history-controls">
        {kind === 'terminal' && <button type="button" disabled={disabled || plan.phase === 'evidence_invalid'} onClick={() => void readTail()}>读取保留终端日志</button>}
        {kind === 'terminal' && onTerminalSelect && sameCase && plan.owner_available && plan.alive === true && <button type="button" disabled={disabled} onClick={() => onTerminalSelect(id)}>连接已有终端</button>}
        {kind === 'viz' && onVizSelect && sameCase && plan.owner_available && plan.alive === true && <button type="button" disabled={disabled} onClick={() => onVizSelect(Number(id))}>打开已有可视化</button>}
        {kind === 'viz' && onVizReopen && sameCase && plan.cleanup_verified === true && <button type="button"
          disabled={disabled || vizOpening} onClick={() => onVizReopen(Number(id))}>按原数据源启动新会话</button>}
        {canClose && <button type="button" disabled={disabled} onClick={() => void closeSelectedSession()}>关闭此{kindNames[kind]}会话</button>}
        {kind === 'cli' && onConversationSelect && plan.conversation_id && sameCase && <button type="button" disabled={disabled}
          onClick={() => { void onConversationSelect(plan.conversation_id!, plan.runtime).catch(failure => setActionError(sessionHistoryError(failure))); }}>查看所属对话</button>}
        {plan.available_actions.includes('reconcile_scope') && <button type="button" disabled={disabled} onClick={() => void act('reconcile_scope')}>核验并接续范围收尾</button>}
        {plan.available_actions.includes('reconcile_initialization') && <button type="button" disabled={disabled} onClick={() => void act('reconcile_initialization')}>核验原初始化占用</button>}
      </div>
      <p ref={feedback} tabIndex={-1} role={actionError ? 'alert' : 'status'} aria-live="polite">
        {busy === 'close_session' ? `正在关闭此${kindNames[kind]}会话…` : busy ? '正在核验操作，请等待回执…' : actionError || notice}
      </p>
      {tail !== null && <details open><summary>原终端保留日志</summary><pre className="native-session-history-tail">{tail || '原记录没有保留输出。'}</pre></details>}
      {plan.available_actions.includes('retain_for_review') && !plan.review && <form onSubmit={event => { event.preventDefault(); void act('retain_for_review'); }}>
        <h4>保留现场并登记核查</h4>
        <p>原会话、日志和文件保留。登记不会补造停止证明，也不会授权发布、删除或控制未知进程。</p>
        <label>核查说明（可选）<textarea rows={3} maxLength={2000} disabled={!!busy} value={note} onChange={event => setNote(event.target.value)} /></label>
        <button type="submit" disabled={disabled}>登记人工核查</button>
      </form>}
      {plan.review && <p>当前快照已登记人工核查，凭证 {plan.review.id}。原限制保持。</p>}
      {plan.reviews.length > 0 && <details><summary>查看保留的核查记录（{plan.reviews.length} 条）</summary>
        <ul>{plan.reviews.map(review => <li key={review.id}><p>{new Date(review.recorded_at).toLocaleString()} · {review.id}</p>
          {review.note && <p>{review.note}</p>}<p className="native-session-history-muted">{review.snapshot_sha256 === plan.snapshot_sha256 ? '关联当前原始快照' : '关联此前保留快照'} · 未授予执行权限</p></li>)}</ul>
      </details>}
      {plan.links && <>
        <SessionLinkEditor kind={kind} source={plan} candidates={candidates} disabled={disabled}
          onBusy={setLinkBusy} onDone={refresh} />
        {plan.links.length > 0 && <details><summary>查看新旧会话关联（{plan.links.length} 条）</summary>
          <ul>{plan.links.map(link => <li key={link.id}>
            <p>原会话 {link.source_id} → 后续会话 {link.successor_id}</p>
            {link.note && <p>{link.note}</p>}
            <p className="native-session-history-muted">凭证 {link.id} · 只记录关联，不继承模型上下文或执行权限。</p>
            <button type="button" disabled={disabled} onClick={() => { void onRelatedSelect(link.source_id === id ? link.successor_id : link.source_id); }}>
              {link.source_id === id ? '查看后续会话' : '查看原会话'}</button>
          </li>)}</ul>
        </details>}
      </>}
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

function SessionLinkEditor({ kind, source, candidates, disabled, onBusy, onDone }: {
  kind: NativeKind; source: SessionHistoryPlan; candidates: SessionHistoryRow[]; disabled: boolean;
  onBusy: (busy: boolean) => void; onDone: () => Promise<void>;
}) {
  const [successor, setSuccessor] = useState('');
  const [note, setNote] = useState('');
  const [preview, setPreview] = useState<SessionLinkPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const controller = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const mounted = useRef(true);
  const feedback = useRef<HTMLParagraphElement>(null);
  useEffect(() => { onBusy(busy); }, [busy, onBusy]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current?.abort(); }; }, []);
  const choices = candidates.filter(row => row.id !== source.id && row.case_id === source.case_id && row.phase !== 'evidence_invalid');
  const existing = source.links?.find(link => link.source_id === source.id && link.successor_id === successor) ?? preview?.existing;
  async function perform(confirm: boolean) {
    if (mutation.current || !successor || confirm && !preview) return;
    mutation.current = true; const request = new AbortController(); controller.current = request;
    setBusy(true); setError(''); setNotice('');
    try {
      const prefix = `/native-sessions/${kind}/${encodeURIComponent(source.id)}`;
      if (!confirm) {
        const value = await sessionHistoryRequest<SessionLinkPlan>(prefix + '/successors/' + encodeURIComponent(successor), { signal: request.signal });
        if (mounted.current) setPreview(value);
      } else {
        const result = await sessionHistoryRequest<SessionLink>(prefix + ':link-successor', { signal: request.signal,
          body: { action: 'link_existing_successor', successor_id: successor,
            source_snapshot_sha256: preview!.source_snapshot_sha256, successor_identity_sha256: preview!.successor_identity_sha256, note } });
        if (mounted.current) { setNotice(`已记录关联，凭证 ${result.id}。原记录与停止限制保持。`); await onDone(); }
      }
    } catch (failure) {
      if (mounted.current && !request.signal.aborted) { setError(sessionHistoryError(failure)); if (confirm) await onDone(); }
    } finally { mutation.current = false; if (mounted.current) { setBusy(false); feedback.current?.focus(); } }
  }
  return <section className="native-session-link" aria-busy={busy}>
    <h4>关联已存在的后续会话</h4>
    <p>后续会话须晚于原会话。此操作只记录你确认的关系；两边使用各自的输入和线程，不会启动任务、继承工具上下文或放宽原停止限制。</p>
    {!choices.length ? <p className="native-session-history-muted">当前已读取的历史中没有其他可核验会话。可在上方读取更多历史后重新选择。</p> : <>
      <label>选择已有后续会话<select value={successor} disabled={disabled || busy} onChange={event => { setSuccessor(event.target.value); setPreview(null); setError(''); setNotice(''); }}>
        <option value="">请选择已存在的会话</option>
        {choices.map(row => <option key={row.id} value={row.id}>{row.id.slice(0, 12)} · {sessionPhase(row.phase)}</option>)}
      </select></label>
      <label>关联说明（可选）<textarea rows={2} maxLength={2000} value={note} disabled={disabled || busy} onChange={event => setNote(event.target.value)} /></label>
      <button type="button" disabled={disabled || busy || !successor} onClick={() => void perform(false)}>核验关联方案</button>
      {preview && <p>原会话 {preview.source.id} → 后续会话 {preview.successor.id}。原记录版本 {preview.source.version}，后续记录状态为{sessionPhase(preview.successor.phase)}。</p>}
      {existing ? <p>此关系已登记，凭证 {existing.id}。可在下方查看新旧会话记录。</p> : preview && <button type="button" disabled={disabled || busy} onClick={() => void perform(true)}>确认记录关联</button>}
    </>}
    <p ref={feedback} tabIndex={-1} role={error ? 'alert' : 'status'} aria-live="polite">{busy ? '正在核验会话关联…' : error || notice}</p>
  </section>;
}
