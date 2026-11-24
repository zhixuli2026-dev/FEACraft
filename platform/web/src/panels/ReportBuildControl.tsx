import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { activatePanel } from '../shell/dock';
import './ReportBuildControl.css';

interface Pending { key: string; jobId?: number }
interface Output { path: string; size: number }
interface Job { id: number; case_id: number; status: string; phase?: string;
  completion_pending?: boolean; recovery?: { state?: string } }

/** A saved submission key allows an explicit retry after an ambiguous response. */
export default function ReportBuildControl({ caseId, userId }: { caseId: number; userId: number }) {
  const storageKey = `vf-report-build:${userId}:${caseId}`;
  const [pending, setPending] = useState<Pending | null>(null);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [outputs, setOutputs] = useState<Output[]>([]);
  const [acting, setActing] = useState(false);
  const current = useRef<Pending | null>(null);
  const epoch = useRef(0);
  const busy = useRef(false);
  const polling = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requests = useRef(new Set<AbortController>());

  const persist = (value: Pending | null) => {
    current.current = value; setPending(value);
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* Server idempotency still protects an explicit retry in this mount. */ }
  };
  async function request<T>(method: string, path: string, key?: string): Promise<T> {
    const controller = new AbortController(); requests.current.add(controller);
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const token = localStorage.getItem('vf_token');
      const response = await fetch('/api/v1' + path, { method, signal: controller.signal,
        headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(key ? { 'Idempotency-Key': key } : {}), ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}) },
        ...(method === 'POST' ? { body: '{}' } : {}) });
      const data = await response.json();
      if (!response.ok) throw new ApiError(response.status, data.detail ?? data);
      return data as T;
    } finally { clearTimeout(timeout); requests.current.delete(controller); }
  }
  async function loadOutputs(generation: number) {
    const data = await request<{ outputs: Output[] }>('GET', `/cases/${caseId}/report/outputs`);
    if (epoch.current === generation) setOutputs(data.outputs);
  }
  async function poll(generation: number) {
    const jobId = current.current?.jobId;
    if (!jobId || polling.current || epoch.current !== generation) return;
    if (timer.current) clearTimeout(timer.current);
    polling.current = true;
    try {
      const job = await request<Job>('GET', `/jobs/${jobId}`);
      if (epoch.current !== generation) return;
      if (job.case_id !== caseId || job.id !== jobId) throw new Error('作业与当前算例不一致');
      setError('');
      if (job.recovery?.state === 'requires_attention') {
        setStatus(`作业 #${jobId} 需要恢复检查，请在作业面板处理。`); persist(null); return;
      }
      if (job.completion_pending || ['queued', 'running'].includes(job.status) || job.phase === 'cancelling') {
        setStatus(`作业 #${jobId} · ${job.completion_pending ? '正在确认结果发布' : job.phase === 'cancelling'
          ? '正在取消并等待收尾' : job.status === 'queued' ? '等待执行' : '正在生成报告'}`);
        timer.current = setTimeout(() => void poll(generation), 1500);
        return;
      }
      const labels: Record<string, string> = { succeeded: '构建完成', failed: '构建失败', cancelled: '已取消', interrupted: '已中断' };
      if (!labels[job.status]) throw new Error(`无法识别作业状态：${job.status}`);
      setStatus(`作业 #${jobId} · ${labels[job.status]}${job.status !== 'succeeded' ? '，可在作业面板查看详情。' : ''}`);
      persist(null);
      await loadOutputs(generation);
      if (epoch.current === generation && useStore.getState().currentCase?.id === caseId) void useStore.getState().loadTree();
    } catch (cause) {
      if (epoch.current === generation) setError(`状态或产物读取失败：${cause instanceof Error ? cause.message : String(cause)}。刷新只查询，不会重新构建。`);
    } finally { if (epoch.current === generation) polling.current = false; }
  }
  async function submit() {
    if (busy.current || current.current?.jobId) return;
    const generation = epoch.current;
    const submission = current.current ?? { key: crypto.randomUUID() };
    persist(submission); busy.current = true; setActing(true); setError(''); setStatus('正在提交报告构建…');
    try {
      const job = await request<{ job_id: number }>('POST', `/cases/${caseId}/report:build`, submission.key);
      if (epoch.current !== generation) return;
      if (!Number.isSafeInteger(job.job_id) || job.job_id < 1) throw new Error('未收到有效的作业编号');
      persist({ ...submission, jobId: job.job_id }); void poll(generation);
    } catch (cause) {
      if (epoch.current !== generation) return;
      if (cause instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(cause.status)) {
        persist(null); setStatus('报告未提交');
        setError(`构建请求被拒绝：${cause.message}。修正后可重新构建。`);
      } else {
        setStatus('尚未确认提交结果');
        setError('连接中断或服务未返回有效结果。请确认提交结果；将沿用同一请求，不会重复创建作业。');
      }
    } finally { if (epoch.current === generation) { busy.current = false; setActing(false); } }
  }
  async function cancel() {
    const jobId = current.current?.jobId;
    if (!jobId || busy.current) return;
    const generation = epoch.current;
    busy.current = true; setActing(true); setError('');
    try {
      await request('POST', `/jobs/${jobId}:cancel`);
      if (epoch.current !== generation) return;
      setStatus(`作业 #${jobId} · 已请求取消，等待收尾`); void poll(generation);
    } catch (cause) {
      if (epoch.current === generation) setError(`取消请求未确认：${cause instanceof Error ? cause.message : String(cause)}。请刷新作业状态。`);
    } finally { if (epoch.current === generation) { busy.current = false; setActing(false); } }
  }
  useEffect(() => {
    const generation = ++epoch.current;
    let saved: Pending | null = null;
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null');
      if (typeof value?.key === 'string' && /^[0-9a-f-]{36}$/.test(value.key)
          && (value.jobId === undefined || Number.isSafeInteger(value.jobId) && value.jobId > 0)) saved = value;
    } catch { /* An invalid local hint is not a server job. */ }
    current.current = saved; setPending(saved);
    void loadOutputs(generation).catch(() => { if (epoch.current === generation) setError('产物列表读取失败，请刷新。'); });
    if (saved?.jobId) void poll(generation);
    else if (saved) setStatus('上次提交结果尚未确认，请确认提交结果。');
    return () => {
      ++epoch.current;
      if (timer.current) clearTimeout(timer.current);
      requests.current.forEach(controller => controller.abort());
      busy.current = false; polling.current = false;
    };
  }, [caseId, userId]);

  return <section className="report-build-control" aria-label="报告构建" data-testid="report-build-control">
    <div className="report-build-actions">
      <button type="button" data-testid="report-build" disabled={Boolean(pending) || acting} onClick={() => void submit()}>构建 HTML / DOCX</button>
      {pending && !pending.jobId && <button type="button" disabled={acting} onClick={() => void submit()}>确认提交结果</button>}
      {pending?.jobId && <button type="button" disabled={acting} onClick={() => void cancel()}>取消构建</button>}
      <button type="button" onClick={() => { const generation = epoch.current; setError(''); if (current.current?.jobId) void poll(generation);
        else void loadOutputs(generation).catch(() => { if (epoch.current === generation) setError('产物列表读取失败，请重试。'); }); }}>刷新状态与产物</button>
      <button type="button" onClick={() => activatePanel('jobs')}>查看作业</button>
    </div>
    {status && <p role="status" data-testid="report-build-status">{status}</p>}
    {error && <p role="alert" data-testid="report-build-error">{error}</p>}
    <ul aria-label="已生成报告">
      {outputs.filter(output => /\.(html|docx)$/.test(output.path)).map(output => <li key={output.path}>
        <button type="button" onClick={async () => { try {
          const opened = await useStore.getState().openFile(output.path);
          if (opened && useStore.getState().currentCase?.id === caseId) activatePanel('viewer');
        } catch { setError('报告打开失败，请刷新后重试。'); } }}>{output.path.split('/').pop()}</button>
        <span>{(output.size / 1024).toFixed(1)} KB</span>
      </li>)}
    </ul>
  </section>;
}
