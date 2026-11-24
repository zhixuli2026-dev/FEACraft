import { useEffect, useId, useRef, useState } from 'react';
import { api, ApiError, olderConversationPage } from '../api';
import ChatMarkdown from './ChatMarkdown';
import ChatImages, { DraftImages, maxMessageImages, readImageFiles } from './ChatImages';
import { captureProjectCase, clearSubmittedAgentDraft, currentAgentDraft, useStore } from '../store';
import NativeUsage from './NativeUsage';
import NativeModelOptions, { summaryNames, type NativeModelChoices, type NativeModelInfo } from './NativeModelOptions';
import { providerProfilesChanged } from './ProviderProfilesPanel';
import NativeApproval, { type NativeApprovalValue } from './NativeApproval';
import NativeSessionHistory from './NativeSessionHistory';
import useNativeStartup, { startupReady, startupSettled, startupResponseUnknown, type NativeRuntime, type NativeStartup } from '../utils/useNativeStartup';
import { acceptNativeEvent, isTerminal, phaseLabel, readNativeEvents, type NativeEvent, type NativeTurn } from '../utils/nativeChat';
import { observeNativeReplySound, prepareReplyAudio } from '../utils/replySound';
import './nativeChat.css';

interface Props {
  conversationId: number; caseId: number; userId: number; runtime: NativeRuntime;
  profileId: number | null; model: string; permission: string;
  modelChoices: NativeModelChoices;
  onModelChoices: (value: NativeModelChoices) => void;
  onBusy: (busy: boolean) => void;
  onBranch: (cid: number, runtime: string) => Promise<void>;
}
interface NativeFork { id: string; conversation_id: number; source_conversation_id: number; phase: string; runtime: string; error?: string; cleanup_verified: boolean | null }
interface Activity { phase: string; backend: string | null; active_turn: NativeTurn | null; native_fork?: NativeFork; active_fork?: NativeFork; active_session_request?: NativeStartup }
interface Pending { id: string; session: string }
type NativeModelCatalog = { data: NativeModelInfo[]; nextCursor: string | null };
type NativeSandbox = {
  runtime: NativeRuntime;
  legacy_readiness: 'ready' | 'notConfigured' | 'updateRequired' | 'not_applicable';
  windows_override?: 'elevated' | 'unelevated' | null;
};
interface Inspection {
  account: { credential?: { inference_configured: boolean } };
  sandbox: NativeSandbox;
  models: NativeModelCatalog;
}
const modelCatalogs = new Map<string, { catalog: NativeModelCatalog; expiresAt: number }>();
const runtimeLabel: Record<NativeRuntime,string> = { windows: 'Windows', wsl: 'WSL', docker: 'Docker' };
window.addEventListener(providerProfilesChanged, () => modelCatalogs.clear());

function RetainedCaseStage({ sessionId }: { sessionId: string }) {
  const endpoint = `/codex/sessions/${sessionId}/staging`;
  const [changes, setChanges] = useState<{ path:string; after:{ kind:string } | null }[] | null>(null);
  const [preview, setPreview] = useState<{ path:string; content:string } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function read(path?: string) {
    setLoading(true);
    setError('');
    try {
      if (path) setPreview(await api.get(endpoint+'?path='+encodeURIComponent(path)));
      else {
        const value = await api.get<{ changes: NonNullable<typeof changes> }>(endpoint);
        setChanges(value.changes);
      }
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : '暂时无法读取工作副本。');
    } finally {
      setLoading(false);
    }
  }

  return <details className="native-records" onToggle={event => {
    if (event.currentTarget.open && changes === null && !loading) void read();
  }}>
    <summary>查看保留的算例改动</summary>
    {loading && <p role="status">正在读取工作副本…</p>}
    {error && <p role="alert">{error} <button type="button" disabled={loading} onClick={() => void read()}>重试</button></p>}
    {changes?.length === 0 && <p className="native-meta">暂无保留的改动。</p>}
    {!!changes?.length && <ul>{changes.map(change => <li key={change.path}>
      {change.after?.kind === 'file' ?
        <button type="button" disabled={loading} onClick={() => void read(change.path)}>{change.path}</button> :
        <span>{change.path}{change.after === null ? ' · 删除' : ''}</span>}
    </li>)}</ul>}
    {preview && <><p className="native-meta">{preview.path}</p><pre>{preview.content}</pre></>}
  </details>;
}

export default function NativeChatPanel(props: Props) {
  const { conversationId: cid, runtime, caseId, profileId, model, permission, userId, onBusy, modelChoices, onModelChoices } = props;
  const prefix = `/codex/conversations/${cid}`;
  const pendingKey = `vf-native-pending:${userId}:${cid}`;
  const forkKey = `vf-native-fork:${userId}:${cid}`;
  const startup = useNativeStartup(userId, cid);
  const [cancellingStartup, setCancellingStartup] = useState(false);
  const draft = useStore(currentAgentDraft);
  const { input, images: attachments } = draft;
  const submittedDraft = useRef<{ id: string; clear: () => void; observe: (turn: NativeTurn) => void } | null>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const [resume, setResume] = useState(false);
  const [goal, setGoal] = useState(false);
  const [turns, setTurns] = useState<NativeTurn[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [turnId, setTurnId] = useState<string | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [historyPage, setHistoryPage] = useState<string | null>(null);
  const [events, setEvents] = useState<NativeEvent[]>([]);
  const [approvals, setApprovals] = useState<NativeApprovalValue[]>([]);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [recovery, setRecovery] = useState<{ id: string; phase: 'running' | 'done' | 'error' | 'pending'; message: string } | null>(null);
  const [connection, setConnection] = useState('');
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const inspectionDialog = useRef<HTMLDialogElement>(null);
  const inspectionTitle = useId();
  useEffect(() => {
    const dialog = inspectionDialog.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, [inspection]);
  const [cachedCatalog, setCachedCatalog] = useState<{ catalog: NativeModelCatalog; expiresAt: number } | null>(null);
  const catalogKey = `${userId}:${caseId}:${cid}:${runtime}:${profileId}:${model}`;
  useEffect(() => {
    setInspection(null);
    const existing = modelCatalogs.get(catalogKey);
    setCachedCatalog(existing && existing.expiresAt > Date.now() ? existing : null);
    const invalidate = () => { setCachedCatalog(null); setInspection(null); };
    window.addEventListener(providerProfilesChanged, invalidate);
    return () => { window.removeEventListener(providerProfilesChanged, invalidate); };
  }, [catalogKey]);
  useEffect(() => {
    if (!cachedCatalog) return;
    const timer = setTimeout(() => setCachedCatalog(null), Math.max(0, cachedCatalog.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [cachedCatalog]);
  const [pending, setPending] = useState<Pending | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(pendingKey) ?? 'null'); } catch { return null; }
  });
  const [pendingFork, setPendingFork] = useState<{client_request_id: string; source_turn_id: string} | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(forkKey) ?? 'null'); } catch { return null; }
  });
  const forkRef = useRef(pendingFork); forkRef.current = pendingFork;
  const seeded = useRef(false);
  const mounted = useRef(true);
  const actionLock = useRef(false);
  const activeSeen = useRef<string | null>(null);
  const pendingRef = useRef(pending); pendingRef.current = pending;
  const eventsRef = useRef<NativeEvent[]>([]);
  const recordRef = useRef<HTMLDetailsElement>(null);
  const approvalRef = useRef<HTMLDivElement>(null);
  const discovered = useRef(false);
  const olderHistoryLoaded = useRef(false);
  const turn = turns.find(t => t.id === turnId);
  const recoveryTurn = activity?.active_turn && (activity.active_turn.phase === 'recovery_required' || isTerminal(activity.active_turn.phase))
    ? activity.active_turn : null;
  const blocked = !activity || activity.phase !== 'idle';
  const busy = loading || working || blocked || !!pending || !!pendingFork || !!startup.pending && !startupReady(startup.state);
  const settingsBusy = busy || !!startup.pending;
  const latest = turns[0];
  const waitingGoal = !blocked && latest?.runtime === runtime && latest.status === 'waiting_continue' &&
    latest.cleanup_verified === true ? latest : null;
  useEffect(() => { onBusy(settingsBusy); }, [settingsBusy, onBusy]);
  const approvalIds = approvals.map(value => value.id).join(',');
  const waitingForApproval = !!approvalIds && !recoveryTurn;
  useEffect(() => {
    if (waitingForApproval) {
      approvalRef.current?.focus({ preventScroll: true });
      approvalRef.current?.scrollIntoView({ block: 'start' });
    }
  }, [approvalIds, waitingForApproval]);
  useEffect(() => {
    if (!recoveryTurn && !waitingForApproval) recordRef.current?.scrollIntoView({ block: 'start' });
  }, [turnId, recoveryTurn?.id, waitingForApproval]);
  const remember = (value: Pending | null) => {
    pendingRef.current = value;
    if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingKey);
    if (mounted.current) setPending(value);
  };
  const loadHistory = async () => {
    const value = await api.get(`/conversations/${cid}`);
    if (mounted.current) {
      setHistory(old => olderHistoryLoaded.current ? [...old.filter(e => !value.events.some((n: any) => n.id === e.id)), ...value.events] : value.events);
      if (!olderHistoryLoaded.current) setHistoryPage(olderConversationPage(cid, value.event_window));
    }
  };
  const refresh = async () => {
    const [live, page] = await Promise.all([api.get<Activity>(prefix + '/activity'), api.get<{ data: NativeTurn[]; next_cursor: string | null }>(prefix + '/turns')]);
    const startupReceipt = await startup.refresh();
    if (startupReceipt && mounted.current) setError(old => old === 'Error: ' + startupResponseUnknown ? '' : old);
    let recovered: NativeTurn | undefined;
    const unresolved = pendingRef.current;
    if (unresolved) {
      const found = await api.get<{ data: NativeTurn[] }>(prefix + `/turns?client_message_id=${encodeURIComponent(unresolved.id)}`);
      recovered = found.data[0];
      if (recovered) {
        remember(null);
        startup.consume();
        const submitted = submittedDraft.current;
        if (mounted.current) setError('');
        if (submitted?.id === unresolved.id) {
          submitted.observe(recovered);
          submitted.clear();
          submittedDraft.current = null;
        }
      }
    }
    if (!mounted.current) return;
    setPollError('');
    setActivity(live);
    if (live.phase === 'idle') setRecovery(old => old && old.phase !== 'running' && old.phase !== 'done'
      ? { ...old, phase: 'done', message: '会话已就绪，可以继续发送任务。' } : old);
    if (live.native_fork?.phase === 'completed' && !seeded.current) { seeded.current = true; setResume(true); }
    setTurns(old => [...page.data, ...old.filter(t => !page.data.some(n => n.id === t.id))]);
    if (!discovered.current) { setCursor(page.next_cursor); discovered.current = true; }
    const newActive = live.active_turn?.id && live.active_turn.id !== activeSeen.current ? live.active_turn.id : null;
    activeSeen.current = live.active_turn?.id ?? null;
    setTurnId(old => newActive ?? recovered?.id ?? old ?? page.data[0]?.id ?? null);
    if (live.active_turn && !page.data.some(t => t.id === live.active_turn!.id)) setTurns(old => [live.active_turn!, ...old]);
    await loadHistory();
    const forkRequest = forkRef.current;
    if (forkRequest && mounted.current) {
      const found = await api.get<NativeFork[]>(prefix + `/forks?client_request_id=${encodeURIComponent(forkRequest.client_request_id)}`);
      if (found[0] && mounted.current) {
        await props.onBranch(found[0].conversation_id, found[0].runtime);
        sessionStorage.removeItem(forkKey); forkRef.current = null; setPendingFork(null);
      }
    }
  };
  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await refresh(); }
      catch (e) { if (!disposed) { setActivity(null); setPollError(String(e)); } }
      finally {
        if (!disposed) { setLoading(false); timer = setTimeout(poll, 2500); }
      }
    };
    void poll();
    return () => { disposed = true; mounted.current = false; clearTimeout(timer); onBusy(false); };
  }, [cid, runtime]);

  useEffect(() => {
    eventsRef.current = []; setEvents([]); setApprovals([]);
    if (!turnId) { setConnection(''); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let retries = 0;
    const forms = async () => {
      const values = await api.get<NativeApprovalValue[]>(`/codex/turns/${turnId}/approvals`);
      if (!controller.signal.aborted) setApprovals(values.filter(value => value.phase === 'pending'));
    };
    let formQueue = Promise.resolve();
    const connect = async () => {
      if (controller.signal.aborted) return;
      setConnection(retries ? '正在重连事件流…' : '正在读取运行记录…');
      try {
        await forms();
        const token = localStorage.getItem('vf_token');
        const after = eventsRef.current.at(-1)?.seq ?? 0;
        const response = await fetch(`/api/v1/codex/turns/${turnId}/events?after=${after}`, {
          signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!response.ok) throw new ApiError(response.status, (await response.json()).detail);
        setConnection('已连接');
        await readNativeEvents(response, event => {
          if (controller.signal.aborted) return;
          eventsRef.current = acceptNativeEvent(eventsRef.current, event, turnId);
          setEvents(eventsRef.current);
          if (event.type.startsWith('approval.') || event.type === 'stream.done') {
            formQueue = formQueue.then(forms).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
          }
        });
        if (controller.signal.aborted) return;
        await refresh();
        const ended = eventsRef.current.some(event => event.type === 'stream.done');
        if (ended) { setConnection('运行记录已同步'); return; }
        throw new Error('连接已断开，正在读取已提交的事件');
      } catch (e) {
        if (controller.signal.aborted) return;
        setConnection(String(e));
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return;
        retries++; timer = setTimeout(connect, Math.min(1000 * retries, 10000));
      }
    };
    void connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [turnId]);

  const runAction = async (operation: () => Promise<void>) => {
    if (actionLock.current) return;
    actionLock.current = true; setWorking(true); setError('');
    try { await operation(); }
    catch (e) { if (mounted.current) setError(String(e)); }
    finally { actionLock.current = false; if (mounted.current) setWorking(false); }
  };
  const saveSettings = () => api.patch(prefix + '/settings', { provider_profile_id: profileId, model, permission_mode: permission });
  const createSession = async (purpose: 'chat' | 'inspect') => {
    if (!startup.pending) await saveSettings();
    return startup.create(purpose, profileId!, runtime);
  };
  const send = (continueGoal = false) => {
    const previous = continueGoal ? waitingGoal : null;
    if (busy || !profileId || startup.pending?.purpose === 'inspect' ||
        (continueGoal ? !previous : !input.trim() && !attachments.length)) return;
    const project = captureProjectCase();
    if (!project) return;
    const content = previous && !input.trim() ? '请继续上一轮目标。' : input;
    prepareReplyAudio(userId);
    void runAction(async () => {
      setRecovery(null);
      const session = await createSession('chat');
      if (!mounted.current || !project.isCurrent()) return;
      const message = { id: crypto.randomUUID(), session: session.id };
      remember(message);
      const clear = () => clearSubmittedAgentDraft(project, draft);
      let observing = false;
      const observe = (turn: NativeTurn) => {
        if (observing || turn.client_message_id !== message.id || turn.session_id !== message.session) return;
        observing = true;
        void observeNativeReplySound(userId, turn);
      };
      submittedDraft.current = { id: message.id, clear, observe };
      try {
        const value = await api.post<NativeTurn>(`/codex/sessions/${session.id}/turns`, {
          content, client_message_id: message.id, resume: !!previous || resume, goal: !!previous || goal, images: attachments,
          ...(previous ? { goal_source_turn_id: previous.id } : {}),
          model_options: Object.fromEntries(Object.entries(modelChoices).filter(([, value]) => value !== '')),
        });
        observe(value);
        remember(null);
        startup.consume();
        submittedDraft.current = null;
        clear();
        if (mounted.current) { setTurns(old => [{ ...value, runtime }, ...old]); setTurnId(value.id); }
        await refresh();
      } catch (e) {
        // A lost response cannot prove rejection. Keep only its ID and query it;
        // replaying POST or closing its session could repeat/cancel real work.
        if (e instanceof ApiError && [400, 401, 403, 404, 409, 422, 429].includes(e.status)) {
          remember(null);
          submittedDraft.current = null;
          // A second tab may already be using this idempotently recovered CLI.
          // Keep its startup receipt; automatic close could cancel that turn.
          await refresh();
        }
        throw e;
      }
    });
  };
  const inspect = () => void runAction(async () => {
    const session = await createSession('inspect');
    try {
      // CLI metadata calls serialize on the owned actor.
      const account = await api.get<Inspection['account']>(`/codex/sessions/${session.id}/account`);
      const sandbox = await api.get<NativeSandbox>(`/codex/sessions/${session.id}/sandbox`);
      const models: NativeModelCatalog = { data: [], nextCursor: null };
      const cursors = new Set<string>();
      for (let page = 0; page < 4; page++) {
        const cursor = models.nextCursor;
        if (cursor && cursors.has(cursor)) throw new Error('CLI 模型目录返回了重复游标，请稍后重新检查');
        if (cursor) cursors.add(cursor);
        const result = await api.get<typeof models>(`/codex/sessions/${session.id}/models?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
        models.data.push(...result.data); models.nextCursor = result.nextCursor;
        if (!models.nextCursor) break;
      }
      if (mounted.current) {
        modelCatalogs.delete(catalogKey);
        const entry = { catalog: models, expiresAt: Date.now() + 180_000 };
        modelCatalogs.set(catalogKey, entry);
        if (modelCatalogs.size > 32) modelCatalogs.delete(modelCatalogs.keys().next().value!);
        setCachedCatalog(entry);
        setInspection({ account, sandbox, models });
      }
    } finally { await startup.close(); }
  });
  const fork = () => {
    if (!turn || busy) return;
    void runAction(async () => {
      const request = {client_request_id: crypto.randomUUID(), source_turn_id: turn.id};
      sessionStorage.setItem(forkKey, JSON.stringify(request)); forkRef.current = request; setPendingFork(request);
      let accepted = false;
      try {
        const value = await api.post<NativeFork>(prefix + '/fork', request);
        accepted = true;
        await props.onBranch(value.conversation_id, value.runtime);
        sessionStorage.removeItem(forkKey); forkRef.current = null; setPendingFork(null);
      } catch (e) {
        if (!accepted && e instanceof ApiError && [400,401,403,404,409,422,429].includes(e.status)) {
          sessionStorage.removeItem(forkKey); forkRef.current = null; setPendingFork(null);
        }
        throw e;
      }
    });
  };
  const handoff = (boundary: { head_event_id: number } | { before_answer_turn_id: string }) => {
    void runAction(async () => {
      const branch = await api.post<{ id: number }>(`/conversations/${cid}/handoff`, boundary);
      if (mounted.current) await props.onBranch(branch.id, runtime);
    });
  };
  const addImages = (files: File[]) => {
    if (busy || !files.length) return;
    const project = captureProjectCase();
    void runAction(async () => {
      const values = await readImageFiles(files, attachments.length);
      if (!project?.isCurrent()) return;
      useStore.getState().updateAgentDraft(current => {
        if (current.images.length + values.length > maxMessageImages) throw new Error('每条消息最多 3 张图片');
        return { ...current, images: [...current.images, ...values] };
      });
    });
  };
  const respond = async (id: string, body: unknown) => {
    await api.post(`/codex/turns/${turnId}/approvals/${id}:respond`, body);
    const forms = await api.get<NativeApprovalValue[]>(`/codex/turns/${turnId}/approvals`);
    if (mounted.current) setApprovals(forms.filter(value => value.phase === 'pending'));
    await refresh();
  };
  const text = events.filter(e => e.type === 'text.delta').map(e => e.payload.text).join('');
  const reasoning = events.filter(e => e.type === 'reasoning.summary.delta').map(e => e.payload.text).join('');
  const capacityEvent = [...events].reverse().find(e =>
    e.type === 'capacity.backoff' || e.type === 'request.started' || e.type === 'stream.done');
  const backoff = turn && !isTerminal(turn.phase) && capacityEvent?.type === 'capacity.backoff'
    ? capacityEvent.payload : null;
  const requestedOptions = events.find(e => e.type === 'turn.admitted')?.payload.model_options;
  const catalogModel = cachedCatalog?.catalog.data.find((item) => item.model === model || item.id === model);
  const items = new Map<string, Record<string, any>>();
  for (const e of events) {
    if (e.type === 'item.started' || e.type === 'item.completed') items.set(e.payload.id, { ...items.get(e.payload.id), ...e.payload });
    if (e.type === 'tool.output.delta') {
      const item = items.get(e.payload.item_id) ?? { id: e.payload.item_id, type: 'tool' };
      items.set(item.id, { ...item, output: (item.output ?? '') + e.payload.text });
    }
  }
  const delivered = !!turnId && history.some(e => e.payload?.native_execution_id === turnId && e.payload?.role === 'assistant');
  const forkState = activity?.active_fork ?? activity?.native_fork;
  const forkUncertain = !!forkState && ['recovery_required', 'evidence_invalid'].includes(forkState.phase);
  const startupState = startup.state ?? activity?.active_session_request ?? null;
  const starting = !!startupState && ['admitted', 'starting'].includes(startupState.phase);
  const startupUncertain = !!startupState && ['recovery_required', 'evidence_invalid'].includes(startupState.phase);
  const recover = () => {
    if (!recoveryTurn) return;
    const id = recoveryTurn.id;
    void runAction(async () => {
      setRecovery({ id, phase: 'running', message: '正在核验旧运行并保存未同步文件…' });
      try {
        const result = await api.post<{ execution_lock_recovered: boolean; staging_reconciled: boolean }>(`/codex/turns/${encodeURIComponent(id)}:reconcile-staging`);
        if (mounted.current) setRecovery({ id, phase: result.execution_lock_recovered ? 'done' : 'pending',
          message: result.execution_lock_recovered ? '会话已恢复，可以继续发送任务。' : '仍有未完成的收尾。文件已保留，请核对运行记录后重试。' });
      } catch (e) {
        const message = e instanceof ApiError ? e.code === 'staging_conflict'
          ? '算例文件存在冲突，未同步文件已保留。请先核对文件，再重试恢复。'
          : `恢复尚未完成：${e.message}`
          : '恢复结果暂未确认，正在读取最新状态。仍需恢复时可重试，已完成的步骤不会重复执行。';
        if (mounted.current) setRecovery({ id, phase: e instanceof ApiError ? 'error' : 'pending', message });
      } finally {
        await refresh();
      }
    });
  };
  const recoveryMessage = recovery && (!recoveryTurn || recovery.id === recoveryTurn.id) ? recovery : null;
  const stopButton = <button type="button" data-testid="native-stop" disabled={working || !!recoveryTurn ||
    !(activity?.active_turn && !isTerminal(activity.active_turn.phase) || activity?.active_fork && ['admitted','forking','registered'].includes(activity.active_fork.phase))}
    onClick={() => void runAction(async () => {
      if (activity?.active_fork) await api.post(`/codex/forks/${activity.active_fork.id}:cancel`);
      else await api.post(`/codex/turns/${activity!.active_turn!.id}:cancel`);
      await refresh();
    })}>{working ? '处理中…' : '停止执行'}</button>;
  return <div className={`native-chat${recoveryTurn || recoveryMessage ? ' native-chat-recovering' : ''}`} data-testid="native-chat">
    <div className="native-status" role="status">
      <span>{runtimeLabel[runtime]} · {loading ? '正在加载…' : startupUncertain ? '启动结果待核对' : forkUncertain ? '分支结果待核对' : phaseLabel(activity?.phase ?? 'unknown')}</span>
      {!recoveryTurn && (approvals.length > 0 || activity?.active_turn?.phase === 'awaiting_input') && <button onClick={() => {
        if (activity?.active_turn) setTurnId(activity.active_turn.id);
        approvalRef.current?.focus({ preventScroll: true });
        approvalRef.current?.scrollIntoView({ block: 'start' });
      }}>处理待回应请求</button>}
      {waitingForApproval ? stopButton : <button disabled={busy || !profileId || startup.pending?.purpose === 'chat'} onClick={inspect}>{startupReady(startup.state) && startup.pending?.purpose === 'inspect' ? '继续检查运行环境' : '检查运行环境'}</button>}
    </div>
    <NativeSessionHistory key={`${userId}:${caseId}`} userId={userId} caseId={caseId} initialKind="cli" onConversationSelect={props.onBranch} />
    <NativeModelOptions value={modelChoices} change={onModelChoices} model={catalogModel} disabled={busy} />
    {!inspection && !!cachedCatalog?.catalog.data.length &&
      <p className="native-meta" data-testid="native-cached-models">已缓存 {cachedCatalog.catalog.data.length} 个 CLI 模型</p>}
    {error && <div className="native-notice" role="alert">{error}<button onClick={() => void runAction(refresh)}>重新读取状态</button></div>}
    {pollError && <div className="native-notice" role="alert">状态暂时无法读取，正在重试：{pollError}</div>}
    {(recoveryTurn || recoveryMessage) && <section className="native-notice" aria-label="会话恢复" data-testid="native-recovery">
      <p role={recoveryMessage?.phase === 'error' ? 'alert' : 'status'}>{recoveryMessage?.message ?? '当前会话需要恢复。将先核验运行已停止，再保存未同步文件；有冲突时保留文件。'}</p>
      {recoveryTurn && <button type="button" disabled={working} onClick={recover} data-testid="native-recover">
        {recoveryMessage?.phase === 'running' ? '正在恢复…' : recoveryMessage ? '重试恢复会话' : '恢复会话'}
      </button>}
    </section>}
    {!pending && (startup.pending || startupState) && <div className="native-notice" data-testid="native-startup-status" role="status">
      {starting ? '正在启动 CLI，消息尚未发送。' : startupUncertain ? '启动结果待核对，请联系部署管理员。' :
        startupReady(startupState) ? working ? 'CLI 已准备，正在继续本次操作…' : startup.pending?.purpose === 'inspect' ? 'CLI 已准备，可继续检查运行环境。' : 'CLI 已准备。确认任务内容后点击发送。' :
        startupSettled(startupState) ? 'CLI 启动已结束，消息尚未发送。' : '正在查询上次启动请求，消息尚未发送。'}
      {starting && <button disabled={cancellingStartup} onClick={() => {
        setCancellingStartup(true);
        void startup.cancel(startupState!).then(refresh).catch(e => setError(String(e))).finally(() => { if (mounted.current) setCancellingStartup(false); });
      }}>{cancellingStartup ? '正在取消启动…' : '取消启动'}</button>}
      {!!startup.pending && !startupState && <button disabled={working} onClick={() => void runAction(async () => { await startup.retry(); await refresh(); })}>重试同一启动请求</button>}
      {!!startup.pending && (startupReady(startupState) || startupSettled(startupState)) && <button disabled={working || blocked} onClick={() => void runAction(async () => { await startup.close(); await refresh(); })}>{startupSettled(startupState) ? '确认并返回编辑' : '关闭 CLI 会话'}</button>}
      {startupUncertain && <button disabled={working} onClick={() => void runAction(refresh)}>刷新启动状态</button>}
      {startupState?.error && <details><summary>核对信息</summary><p>启动 ID：{startupState.id}</p><p>记录：{startupState.error}</p></details>}
    </div>}
    {pendingFork && <div className="native-notice" role="status">正在核对原生分支请求，确认后自动打开。不会重复创建分支。</div>}
    {forkState && <div className="native-notice" data-testid="native-fork-status" role="status">
      原生分支 · {forkUncertain ? '结果待核对' : phaseLabel(forkState.phase)}
      {activity?.native_fork?.phase === 'completed' ? ' · 已继承原生上下文' : ''}
      {forkUncertain && <>
        <p>分支创建结果尚未确认，当前无法自动恢复。请勿重复分叉，请联系部署管理员核对。</p>
        <button disabled={working} onClick={() => void runAction(refresh)}>刷新分支状态</button>
        {forkState.source_conversation_id !== cid && <button disabled={working} onClick={() => void runAction(() => props.onBranch(forkState.source_conversation_id, forkState.runtime))}>返回来源会话</button>}
      </>}
      {forkState.error && <details><summary>核对信息</summary><p>分支 ID：{forkState.id}</p><p>记录：{forkState.error}</p></details>}
    </div>}
    {pending && <div className="native-notice" role="status">发送结果尚未确认，正在查询原消息。请勿重复发送。
      <button disabled={working} onClick={() => void runAction(async () => {
        await refresh();
        if (!pendingRef.current) return;
        const session = await api.get(`/codex/sessions/${pending.session}`);
        if (!['closed', 'failed'].includes(session.phase) || session.cleanup_verified !== true)
          throw new Error('原运行尚未确认收尾，请继续等待或联系部署管理员检查此会话');
        const found = await api.get(prefix + `/turns?client_message_id=${encodeURIComponent(pending.id)}`);
        if (!found.data.length) remember(null);
      })}>核对未确认消息</button>
    </div>}
    <div className="native-history">
      {waitingForApproval && <div ref={approvalRef} role="region" aria-label="待回应请求" tabIndex={-1}>
        {approvals.map(value => <NativeApproval key={value.id} value={value} readonly={['ask', 'plan'].includes(permission)} respond={respond} />)}
      </div>}
      {inspection && <dialog ref={inspectionDialog} className="native-inspection" aria-labelledby={inspectionTitle}
        onClose={() => setInspection(null)}>
        <div className="native-inspection-header">
          <h2 id={inspectionTitle}>运行环境检查</h2>
          <button type="button" onClick={() => inspectionDialog.current?.close()}>关闭</button>
        </div>
        <dl>
          <dt>调用凭据</dt>
          <dd>{inspection.account.credential?.inference_configured ? 'API 配置已绑定' : '未绑定'}</dd>
          {inspection.sandbox.runtime === 'windows' ? <>
            <dt>Windows 沙箱</dt>
            <dd>{{ ready: '已准备', notConfigured: '尚未配置', updateRequired: '需要更新',
              not_applicable: '状态未知' }[inspection.sandbox.legacy_readiness] ?? '状态未知'}</dd>
            <dt>部署设置</dt>
            <dd>{inspection.sandbox.windows_override
              ? { elevated: '提权模式', unelevated: '非提权运行' }[inspection.sandbox.windows_override]
              : 'Codex 默认模式'}</dd>
          </> : <><dt>执行环境</dt><dd>{runtimeLabel[inspection.sandbox.runtime]}</dd></>}
        </dl>
        {inspection.sandbox.legacy_readiness === 'notConfigured' && <p>请联系部署管理员在 Windows 主机上初始化沙箱，完成后重新检查。</p>}
        {inspection.sandbox.legacy_readiness === 'updateRequired' && <p>请联系部署管理员更新沙箱配置，完成后重新检查。</p>}
        <details>
          <summary>可用模型（{inspection.models.data.length}）</summary>
          <p>{inspection.models.data.map(m => m.displayName || m.id || m.model).join('、') || '未返回模型'}</p>
          {inspection.models.nextCursor && <p className="native-meta">显示目录前 {inspection.models.data.length} 项。可手动填写模型参数。</p>}
        </details>
      </dialog>}
      {historyPage !== null && <button disabled={working} onClick={() => void runAction(async () => {
        const page = await api.get(historyPage);
        olderHistoryLoaded.current = true;
        setHistory(old => [...page.events, ...old.filter(e => !page.events.some((n: any) => n.id === e.id))]);
        setHistoryPage(olderConversationPage(cid, page.event_window));
      })}>加载更早对话</button>}
      {!history.length && !loading && <p className="native-empty">向原生 Agent 发送任务。执行记录会保存在当前会话中，刷新页面后可继续查看。</p>}
      {history.filter(e => e.type === 'message' && ['user', 'assistant'].includes(e.payload?.role) && !e.payload?.tool_calls?.length ||
        e.type === 'system' && ['summary', 'handoff'].includes(e.payload?.kind)).map(e => <article key={e.id} className={`native-message ${e.payload.role === 'user' ? 'native-user' : ''}`}>
        <div className="native-meta">{e.type === 'system' ? '上下文摘要' : e.payload.role === 'user' ? '我' : 'Agent'}
          {e.payload.cancelled ? ' · 已取消' : e.payload.interrupted ? ' · 已中断' : ''}</div>
        <ChatMarkdown text={e.payload.content ?? e.payload.text ?? ''} />
        {e.payload.images?.length > 0 && <ChatImages images={e.payload.images} caseId={caseId} />}
        {e.can_fork === true && <button type="button" data-testid="native-answer-handoff" disabled={working}
          title="把截至这条回答的文字带到新线程" onClick={() => handoff({ head_event_id: e.id })}>从这条回答交接</button>}
      </article>)}
      {turns.length > 0 && <details ref={recordRef} className="native-records" open><summary>原生运行记录</summary>
        <label className="native-question">查看回合<select aria-label="查看原生回合" value={turnId ?? ''} onChange={e => setTurnId(e.target.value)}>
          {turns.map(t => <option key={t.id} value={t.id}>{new Date(t.created_at).toLocaleTimeString()} · {t.runtime ?? runtime} · {phaseLabel(t.status)}</option>)}
        </select></label>
        {cursor && <button disabled={working} onClick={() => void runAction(async () => {
          const page = await api.get(prefix + `/turns?before=${encodeURIComponent(cursor)}`);
          setTurns(old => [...old, ...page.data.filter((n: NativeTurn) => !old.some(t => t.id === n.id))]); setCursor(page.next_cursor);
        })}>更早的运行</button>}
        {turn && <p className="native-meta" data-testid="native-turn-status">{backoff ? '等待模型容量' : phaseLabel(turn.status)} · {connection}
          {turn.cleanup_verified === true ? ' · 收尾已验证' : isTerminal(turn.phase) ? ' · 收尾待核验' : ''}</p>}
        {backoff && <p role="status" data-testid="native-capacity-backoff">
          模型暂时繁忙，正在等待续跑（{backoff.retry}/{backoff.maximum}）。等待约 {Math.ceil(backoff.delay_seconds)} 秒。
        </p>}
        {turn?.error && <p role="alert">{turn.error === 'CodexCapacityRequiresReview'
          ? '模型容量不足，当前回合已有工具执行记录。请核对结果后再继续。'
          : turn.error === 'CodexCapacityFailure' ? '模型容量不足，自动续跑次数已用完。'
            : turn.error === 'DockerCasePublicationConflict' ? '算例在运行期间已有其他修改，已保留原件和容器工作副本。'
            : `运行错误：${turn.error}`}</p>}
        {turn?.error === 'DockerCasePublicationConflict' && <RetainedCaseStage key={turn.session_id} sessionId={turn.session_id} />}
        {turn && <NativeUsage value={turn.usage} finished={isTerminal(turn.phase)} />}
        {requestedOptions && <details className="native-request-options"><summary>本次请求的模型参数</summary>
          <p>推理强度：{requestedOptions.effort ?? '沿用原生设置'}</p>
          <p>推理摘要：{summaryNames[requestedOptions.summary] ?? '沿用原生设置'}</p>
          <p>服务档位：{requestedOptions.service_tier ?? '沿用原生设置'}</p>
        </details>}
        {turn && <div className="native-actions">
          <button type="button" data-testid="native-fork"
            disabled={settingsBusy || turn.phase !== 'completed' || turn.cleanup_verified !== true || turn.id !== latest?.id || turn.runtime !== runtime}
            title="从最后一个已完成的原生回合创建独立线程，保留工具上下文" onClick={fork}>原生分叉</button>
          <button type="button" data-testid="native-turn-handoff" disabled={working}
            title="把截至本轮提问的文字带到新线程" onClick={() => handoff({ before_answer_turn_id: turn.id })}>交接至本轮提问</button>
        </div>}
        {reasoning && <details><summary>推理摘要</summary><pre>{reasoning}</pre></details>}
        {text && !delivered && <article className="native-message" data-testid="native-partial"><div className="native-meta">{turn && isTerminal(turn.phase) ? '未完成的输出' : 'Agent · 正在输出'}</div><ChatMarkdown text={text} /></article>}
        {[...items.values()].filter(item => item.type !== 'agentMessage' && item.type !== 'reasoning').map(item => <details key={item.id} className="native-tool"><summary>{item.type}{item.status ? ` · ${item.status}` : ''}</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>)}
        {events.some(e => e.type === 'context.handoff') && <p className="native-meta">本线程接收了有界文字历史；工具调用与审批不会重放。</p>}
      </details>}
    </div>
    {!waitingForApproval && <form className="native-composer" onSubmit={event => { event.preventDefault(); send(); }}>
      {waitingGoal && <section className="native-notice" aria-label="原生目标" data-testid="native-goal-waiting" data-turn-id={waitingGoal.id}>
        <div role="status">目标回合已结束，等待继续。</div>
        <p className="native-meta">可以补充任务。继续目标会在原线程启动一轮。</p>
        <button type="button" data-testid="native-goal-continue" onClick={() => send(true)}
                disabled={busy || !profileId || startup.pending?.purpose === 'inspect'}>继续目标</button>
      </section>}
      <DraftImages images={attachments} disabled={busy} onRemove={index => useStore.getState().updateAgentDraft(current => ({
        ...current, images: current.images.filter((_, i) => i !== index),
      }))} />
      <label className="native-question">任务<textarea aria-label="原生任务" data-testid="native-input" value={input} disabled={busy}
        placeholder="描述要完成的任务…" maxLength={128 * 1024} onChange={e => {
          const value = e.target.value;
          useStore.getState().updateAgentDraft(current => ({ ...current, input: value }));
        }}
        onPaste={event => { const files = Array.from(event.clipboardData.files); if (files.length) { event.preventDefault(); addImages(files); } }}
        onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} /></label>
      <div className="native-actions">
        <label className="native-check"><input type="checkbox" checked={resume} disabled={busy} onChange={e => setResume(e.target.checked)} />恢复原生线程</label>
        <label className="native-check"><input type="checkbox" checked={goal} disabled={busy} onChange={e => setGoal(e.target.checked)} />目标回合</label>
      </div>
      <p className="native-meta">{startupUncertain ? '当前结果需由部署管理员核对，暂不可继续发送。' : !pending && (startup.pending || startupState) ? '消息尚未发送；刷新页面不会自动发送。' : forkUncertain ? '此分支暂不可继续，请先由管理员核对结果。' : `${resume ? '需保持原环境及连续历史' : '新线程交接文字与历史图片文件位置'} · 关闭页面后任务继续`}</p>
      <div className="native-actions">
        <input ref={imageInput} hidden type="file" multiple accept="image/png,image/jpeg,image/gif,image/webp" data-testid="native-image-input"
          disabled={busy || attachments.length >= maxMessageImages} onChange={event => { addImages(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
        <button type="button" disabled={busy || attachments.length >= maxMessageImages} title="PNG/JPEG/GIF/WEBP，每张最多 4 MiB，每条消息最多 3 张" onClick={() => imageInput.current?.click()}>添加图片</button>
        <button type="submit" className="native-primary" disabled={busy || !profileId || startup.pending?.purpose === 'inspect'
          || (!input.trim() && !attachments.length)}>发送</button>
        {stopButton}
      </div>
    </form>}
  </div>;
}
