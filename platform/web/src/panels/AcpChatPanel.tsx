import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import { captureProjectCase, clearSubmittedAgentDraft, currentAgentDraft, useStore } from '../store';
import { activatePanel } from '../shell/dock';
import ChatMarkdown from './ChatMarkdown';
import './nativeChat.css';

export type AcpHarness = 'opencode' | 'kimi';
type Phase = 'admitted' | 'running' | 'completed' | 'failed' | 'cancelled' | 'recovery_required';
interface Turn {
  id: string;
  thread_id: string;
  client_message_id: string;
  provider_profile_id: number;
  phase: Phase;
  cleanup_verified: boolean;
  created_at: string;
  allow_drafts: boolean;
}
interface DraftApproval { approval_id: string; path: string; preview: string | null; truncated: boolean }
interface SavedDraft { path: string; text: string }
interface CaseInput { path: string }
interface Publication { draft: string; path: string; content_sha256?: string }
interface TurnEvents {
  turn: Turn;
  prompt: string | null;
  events: ({ seq: number; type: string; text?: string; files?: CaseInput[]; draft?: string }
    & Partial<DraftApproval> & Partial<SavedDraft>)[];
  has_more: boolean;
}
interface Submission {
  client_message_id: string;
  provider_profile_id: number;
  thread_id: string | null;
  content: string;
  allow_drafts: boolean;
  case_files: string[];
}
interface Profile { id: number; name: string; default_model: string; has_key: boolean; shared?: boolean }
interface Props {
  conversationId: number;
  caseId: number;
  harness: AcpHarness;
  profiles: Profile[];
  initialProfileId: number | null;
  onBusy: (busy: boolean) => void;
}

const phaseLabels: Record<Phase, string> = {
  admitted: '正在启动', running: '正在回复', completed: '回复完成',
  failed: '回复失败', cancelled: '已停止', recovery_required: '需要核对运行状态',
};
const isActive = (turn: Turn) => turn.phase === 'admitted' || turn.phase === 'running';

function DraftPublication({ turnId, draft, targets, receipts, onPublished }: {
  turnId: string;
  draft: string;
  targets: CaseInput[];
  receipts: Publication[];
  onPublished: (receipt: Publication) => void;
}) {
  const [target, setTarget] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const editor = useStore(state => state.openFiles[target]);
  const alreadyPublished = receipts.some(item => item.path === target);
  const blocked = !target || pending || alreadyPublished || editor?.dirty || editor?.saving;
  const availableTargets = targets.filter(file => !receipts.some(item => item.path === file.path));

  async function publish() {
    if (blocked) return;
    const project = captureProjectCase();
    const editorId = editor?.editorId;
    setPending(true);
    setError('');
    let receipt: Publication;
    try {
      receipt = await api.post<Publication>(`/acp/turns/${turnId}/drafts:publish`, { draft, path:target });
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : '发布未确认，可以重试。');
      return;
    } finally {
      setPending(false);
    }
    onPublished(receipt);
    setTarget('');
    if (editorId && project?.isCurrent()) {
      const state = useStore.getState();
      try {
        await state.inspectServerVersion(target);
        const current = useStore.getState().openFiles[target];
        if (project.isCurrent() && current?.editorId === editorId && !current.dirty && !current.saving
            && current.serverVersion?.content_sha256 === receipt.content_sha256) {
          state.resolveFileConflict(target, false);
        }
      } catch {
        if (project.isCurrent()) setError('算例已发布。重新打开文件以刷新编辑器。');
      }
    }
  }

  return <>
    {receipts.filter(item => item.draft === draft).map(item => <p className="native-meta" role="status" key={item.path}>
      已发布到 {item.path}
    </p>)}
    {availableTargets.length > 0 && <div className="native-actions">
      <label className="native-actions">替换算例文件
        <select value={target} aria-label={`${draft} 的发布目标`} disabled={pending}
          onChange={event => { setTarget(event.target.value); setError(''); }}>
          <option value="">选择发布目标</option>
          {availableTargets.map(item => <option key={item.path} value={item.path}>{item.path}</option>)}
        </select>
      </label>
      <button type="button" disabled={blocked} onClick={publish}>{pending ? '正在发布…' : '发布到算例'}</button>
    </div>}
    {target && (editor?.dirty || editor?.saving) && <p className="native-meta">请先处理编辑器里的未保存更改。</p>}
    {error && <p role="alert">{error}</p>}
  </>;
}

export default function AcpChatPanel({ conversationId, caseId, harness, profiles, initialProfileId, onBusy }: Props) {
  const endpoint = `/acp/conversations/${conversationId}/${harness}/turns`;
  const draft = useStore(currentAgentDraft);
  const updateDraft = useStore(state => state.updateAgentDraft);
  const activeFile = useStore(state => state.activePublication ? null : state.activeFile);
  const activeEditor = useStore(state => state.activeFile ? state.openFiles[state.activeFile] : undefined);
  const openFiles = useStore(state => state.openFiles);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [selected, setSelected] = useState('');
  const [profile, setProfile] = useState<number | null>(null);
  const [contractProfile, setContractProfile] = useState<number | null>(null);
  const [startNew, setStartNew] = useState(false);
  const [draftMode, setDraftMode] = useState<boolean | null>(null);
  const [approvals, setApprovals] = useState<Record<string, DraftApproval>>({});
  const [deciding, setDeciding] = useState(false);
  const [savedDrafts, setSavedDrafts] = useState<SavedDraft[]>([]);
  const [caseInputLimit, setCaseInputLimit] = useState(0);
  const [caseFiles, setCaseFiles] = useState<string[]>([]);
  const [loadedInputs, setLoadedInputs] = useState<CaseInput[]>([]);
  const [publications, setPublications] = useState<Publication[]>([]);
  const [prompt, setPrompt] = useState('');
  const [reply, setReply] = useState('');
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState(false);
  const [reload, setReload] = useState(0);
  const [stopPending, setStopPending] = useState(false);
  const inFlight = useRef(false);
  const submitted = useRef<Submission | null>(null);
  const history = useRef<HTMLDivElement>(null);
  const attachmentPanel = useRef<HTMLDetailsElement>(null);
  const followsLatest = useRef(true);
  const active = turns.find(isActive);
  const latest = turns[0];
  const shown = turns.find(turn => turn.id === selected);
  const available = profiles.filter(item => !item.shared && item.has_key && item.default_model);
  const profileId = profile ?? contractProfile ?? latest?.provider_profile_id ?? initialProfileId;
  const chosen = available.find(item => item.id === profileId);
  const allowDrafts = draftMode ?? latest?.allow_drafts ?? false;
  const resume = !startNew && chosen && turns.find(turn => turn.provider_profile_id === chosen.id && turn.allow_drafts === allowDrafts);
  const busy = working || !!active;
  const attachmentUnavailable = !activeFile || activeEditor?.kind !== 'text' || activeEditor.dirty
    || activeEditor.saving || caseFiles.includes(activeFile) || caseFiles.length >= caseInputLimit;
  const dirtyAttachment = caseFiles.some(path => openFiles[path]?.dirty || openFiles[path]?.saving);

  useEffect(() => {
    onBusy(busy);
    return () => onBusy(false);
  }, [busy, onBusy]);

  useEffect(() => {
    const controller = new AbortController();
    api.get<{selected: string | null; provider_profile_id: number | null; applied: boolean}>(
      `/cases/${caseId}/cli-contract`, undefined, controller.signal).then(state => {
      setContractProfile(state.applied && state.selected === harness ? state.provider_profile_id : null);
    }).catch(() => {});
    return () => controller.abort();
  }, [caseId, harness, reload]);

  useEffect(() => {
    const controller = new AbortController();
    api.get<{ case_input_limit: number }>(`/acp/conversations/${conversationId}/capabilities`,
      undefined, controller.signal).then(value => setCaseInputLimit(value.case_input_limit)).catch(() => {});
    return () => controller.abort();
  }, [conversationId, reload]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api.get<Turn[]>(endpoint, undefined, controller.signal).then(items => {
      setTurns(items);
      setSelected(items.find(isActive)?.id ?? items[0]?.id ?? '');
      const previous = submitted.current;
      if (previous && items.some(item => item.client_message_id === previous.client_message_id)) {
        const project = captureProjectCase();
        const current = currentAgentDraft(useStore.getState());
        if (project && current.input.trim() === previous.content && !current.images.length) {
          clearSubmittedAgentDraft(project, current);
        }
        setCaseFiles([]);
        if (attachmentPanel.current) attachmentPanel.current.open = false;
      }
      submitted.current = null;
    }).catch(failure => {
      if (!controller.signal.aborted) setError(String(failure));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [endpoint, reload]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let cursor = 0;
    let retry: ReturnType<typeof setTimeout>;
    setReadError(false);
    setPrompt('');
    setReply('');
    setApprovals({});
    setSavedDrafts([]);
    setLoadedInputs([]);
    setPublications([]);
    followsLatest.current = true;
    async function read() {
      try {
        const page = await api.get<TurnEvents>(`/acp/turns/${selected}/events?after=${cursor}`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        if (page.prompt !== null) setPrompt(page.prompt);
        const text = page.events.filter(event => event.type === 'agent_message_chunk').map(event => event.text).join('');
        if (text) setReply(old => old + text);
        const decisions = page.events.filter(event => ['permission', 'permission_decision'].includes(event.type));
        if (decisions.length) setApprovals(old => {
          const updated = { ...old };
          for (const event of decisions) {
            if (!event.approval_id) continue;
            if (event.type === 'permission' && event.path && event.preview !== undefined) {
              updated[event.approval_id] = { approval_id:event.approval_id, path:event.path,
                preview:event.preview, truncated:!!event.truncated };
            } else {
              delete updated[event.approval_id];
            }
          }
          return updated;
        });
        const drafts = page.events.flatMap(event => event.type === 'draft' && event.path && event.text !== undefined
          ? [{ path:event.path, text:event.text }] : []);
        if (drafts.length) setSavedDrafts(old => [...old, ...drafts]);
        for (const event of page.events) {
          if (event.type === 'case_inputs' && event.files) setLoadedInputs(event.files);
          if (event.type === 'draft_published' && event.draft && event.path) {
            const saved = { draft:event.draft, path:event.path };
            setPublications(old => [...old.filter(item => item.path !== saved.path), saved]);
          }
        }
        cursor = page.events.at(-1)?.seq ?? cursor;
        setTurns(items => items.map(item => item.id === selected ? page.turn : item));
        setReadError(false);
        if (!isActive(page.turn)) {
          setStopPending(false);
          setApprovals({});
        }
        if (page.has_more || isActive(page.turn)) retry = setTimeout(read, page.has_more ? 0 : 600);
      } catch {
        if (controller.signal.aborted) return;
        setReadError(true);
        retry = setTimeout(read, 1700);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(retry); };
  }, [selected, reload]);

  useEffect(() => {
    if (followsLatest.current && history.current) history.current.scrollTop = history.current.scrollHeight;
  }, [reply, prompt, approvals, savedDrafts]);

  async function send(event: FormEvent) {
    event.preventDefault();
    const project = captureProjectCase();
    if (inFlight.current || busy || !chosen || !draft.input.trim() || draft.images.length
        || dirtyAttachment || !project) return;
    const content = draft.input.trim();
    const thread = resume ? resume.thread_id : null;
    const previous = submitted.current;
    if (previous && (previous.provider_profile_id !== chosen.id || previous.thread_id !== thread
        || previous.content !== content || previous.allow_drafts !== allowDrafts
        || JSON.stringify(previous.case_files) !== JSON.stringify(caseFiles))) {
      setError('上次提交的回执尚未确认。请先刷新回合记录。');
      return;
    }
    const submission = previous ?? { client_message_id: crypto.randomUUID(), provider_profile_id: chosen.id,
      thread_id: thread, content, allow_drafts:allowDrafts, case_files: caseFiles };
    submitted.current = submission;
    inFlight.current = true;
    setWorking(true);
    setError('');
    try {
      const item = await api.post<Turn>(endpoint, submission);
      setTurns(items => [item, ...items.filter(turn => turn.id !== item.id)].slice(0, 20));
      setSelected(item.id);
      clearSubmittedAgentDraft(project, draft);
      setCaseFiles([]);
      if (attachmentPanel.current) attachmentPanel.current.open = false;
      submitted.current = null;
      setStartNew(false);
    } catch (failure) {
      if (failure instanceof ApiError && failure.status < 500) submitted.current = null;
      setError(failure instanceof ApiError ? failure.message : '未收到回执。保留原消息可用同一编号重试。');
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  }

  async function stop() {
    if (!active || stopPending) return;
    setStopPending(true);
    setError('');
    try {
      const item = await api.post<Turn & {stop_requested?: boolean}>(`/acp/turns/${active.id}:cancel`);
      if (isActive(item) && !item.stop_requested) {
        setError('执行进程尚未确认停止请求，请查看当前状态后再试。');
        setStopPending(false);
      }
    } catch (failure) {
      setError(String(failure));
      setStopPending(false);
    }
  }

  async function decide(approval: DraftApproval, decision: 'approve' | 'reject') {
    if (!active || deciding || stopPending) return;
    setDeciding(true);
    setError('');
    try {
      await api.post(`/acp/turns/${active.id}/approvals/${approval.approval_id}`, { decision });
      setApprovals(old => {
        const pending = { ...old };
        delete pending[approval.approval_id];
        return pending;
      });
    } catch (failure) {
      setError(String(failure));
    } finally {
      setDeciding(false);
    }
  }

  return <section className="native-chat" aria-label={harness === 'opencode' ? 'OpenCode 对话' : 'Kimi Code 对话'}>
    <div className="native-status">
      <span role="status">{loading ? '正在读取回合…' : shown ? phaseLabels[shown.phase] : '原生问答'}</span>
      <label className="native-actions">模型配置
        <select aria-label="原生模型配置" value={chosen?.id ?? ''} disabled={busy || loading}
          onChange={event => { setProfile(Number(event.target.value)); setStartNew(false); }}>
          <option value="" disabled>选择本人 API 配置</option>
          {available.map(item => <option value={item.id} key={item.id}>{item.name} · {item.default_model}</option>)}
        </select>
      </label>
    </div>
    <div className="native-history" ref={history} aria-label="原生回合记录"
      onScroll={({ currentTarget: node }) => { followsLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 32; }}>
      {turns.length > 1 && <label className="native-actions">查看回合
        <select aria-label="查看原生回合" value={selected} disabled={!!active} onChange={event => setSelected(event.target.value)}>
          {turns.map(item => <option value={item.id} key={item.id}>
            {new Date(item.created_at).toLocaleString()} · {phaseLabels[item.phase]}
          </option>)}
        </select>
      </label>}
      {prompt && <div className="native-message native-user">{prompt}</div>}
      {loadedInputs.length > 0 && <details className="native-records">
        <summary>本回合的算例文件</summary>
        <ul>{loadedInputs.map(item => <li key={item.path}>{item.path}</li>)}</ul>
      </details>}
      {reply && <div className="native-message" data-testid="acp-reply"><ChatMarkdown text={reply} streaming={!!active} /></div>}
      {Object.values(approvals).map(approval => <section className="native-approval" key={approval.approval_id}
        aria-label={`草稿审批 ${approval.path}`}>
        <strong>{approval.path}</strong>
        {approval.preview !== null ? <details>
          <summary>查看草稿预览</summary>
          <pre>{approval.preview}</pre>
          {approval.truncated && <p className="native-meta">预览显示前 8000 字符。</p>}
        </details> : <p className="native-meta">内容将在写入后显示。</p>}
        <div className="native-actions">
          <button type="button" onClick={() => decide(approval, 'approve')} disabled={deciding || stopPending}>允许写入一次</button>
          <button type="button" onClick={() => decide(approval, 'reject')} disabled={deciding || stopPending}>拒绝</button>
        </div>
      </section>)}
      {savedDrafts.map(item => <details key={`${selected}:${item.path}`} className="native-records">
        <summary>已保存草稿 · {item.path}</summary>
        <pre>{item.text}</pre>
        {shown?.phase === 'completed' && loadedInputs.length > 0 &&
          <DraftPublication turnId={selected} draft={item.path} receipts={publications}
            targets={loadedInputs.filter(file => file.path.toLowerCase() !== '0-casedict/casedict')}
            onPublished={saved => setPublications(old => [...old.filter(row => row.path !== saved.path), saved])} />}
      </details>)}
      {!loading && !turns.length && <p className="native-empty">选择 API 配置后发送文本消息即可开始问答。</p>}
      {!loading && !available.length && <button type="button" onClick={() => activatePanel('providers')}>打开模型提供商</button>}
      {readError && <p className="native-meta" role="status">回合记录暂不可读，正在重新连接。</p>}
    </div>
    {(error || shown?.phase === 'recovery_required') && <div className="native-notice" role="alert">
      {error || '执行结束尚未核验。此会话的执行状态需要恢复。'}
      <button type="button" onClick={() => setReload(counter => counter + 1)}>刷新回合记录</button>
    </div>}
    <form className="native-composer" onSubmit={send}>
      <label className="native-question">文本消息
        <textarea aria-label="原生文本消息" value={draft.input} maxLength={256000}
          disabled={working} onChange={event => updateDraft(current => ({ ...current, input:event.target.value }))} />
      </label>
      {draft.images.length > 0 && <p className="native-meta" role="status">当前草稿含图片。请在 API Agent 或 Codex 中发送。</p>}
      {caseInputLimit > 0 && <details className="native-case-inputs" ref={attachmentPanel}>
        <summary>附加算例文件{caseFiles.length > 0 && `（${caseFiles.length}）`}</summary>
        <div className="native-actions">
          <button type="button" disabled={busy || attachmentUnavailable}
            onClick={() => { if (activeFile) setCaseFiles(items => [...items, activeFile]); }}>
            附加当前文件
          </button>
        </div>
        <p className="native-meta">在编辑器打开并保存文本后附加。所选内容将提供给模型。</p>
        {caseFiles.length > 0 && <ul className="native-case-file-list">
          {caseFiles.map(path => <li key={path}>
            <span>{path}</span>
            <button type="button" aria-label={`移除 ${path}`} disabled={busy}
              onClick={() => setCaseFiles(items => items.filter(file => file !== path))}>移除</button>
          </li>)}
        </ul>}
        {dirtyAttachment && <p className="native-meta" role="status">请先保存已附加文件的更改。</p>}
      </details>}
      <div className="native-actions">
        <label className="native-check" title="允许模型申请写入私有文本草稿，每次写入都需批准">
          <input type="checkbox" checked={allowDrafts} onChange={event => setDraftMode(event.target.checked)} disabled={busy} />
          草稿审批
        </label>
        {latest && <label className="native-check">
          <input type="checkbox" checked={startNew} onChange={event => setStartNew(event.target.checked)} disabled={busy} />
          开始新线程
        </label>}
        {active && <button type="button" onClick={stop} disabled={stopPending}>{stopPending ? '正在停止…' : '停止执行'}</button>}
        <button type="submit" className="native-primary" disabled={loading || busy || !chosen || !draft.input.trim()
          || draft.images.length > 0 || dirtyAttachment || latest?.phase === 'recovery_required'}>发送</button>
      </div>
    </form>
  </section>;
}
