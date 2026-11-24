import { useEffect, useRef, useState } from 'react';
import { isCaseReadOnly } from '../api';
import { useStore } from '../store';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { saveDerivative } from '../utils/reportDerivative';
import { readCaseSelection, saveCaseSelection } from '../utils/viewSelection';
import './RemoteJobs.css';

type Profile = { name: string; scheduler: 'slurm' | 'pbs'; queue: string; max_seconds: number };
type FileReceipt = { size: number; sha256: string };
type Run = { id: string; case_id: number; profile: string; scheduler_id: string | null; state: string;
  script: string; source_run_id: string | null; observation: { results?: Record<string, FileReceipt>; reason?: string } };
type Submit = { profile: string; script: string; input_paths: string[]; output_paths: string[]; timeout_seconds: number;
  restart?: { run_id: string; files: Array<{ input_path: string; output_path: string }> } };
type Pending = { key: string; body: Submit };

const terminal = new Set(['succeeded', 'failed', 'cancelled']);
const labels: Record<string, string> = { staging: '准备输入', queued: '排队', running: '运行中',
  awaiting_receipt: '等待提交回执', awaiting_scheduler: '等待调度状态', cancelling: '正在取消',
  succeeded: '成功', failed: '失败', cancelled: '已取消' };
const lines = (text: string) => text.split('\n').map(path => path.trim()).filter(Boolean);

export default function RemoteJobs({ caseId, userId }: { caseId: number; userId: number }) {
  const readonly = useStore(state => isCaseReadOnly(state.currentCase,state.currentWs));
  const storageKey = `vf-hpc-submit:${userId}:${caseId}`;
  const endpoint = `/cases/${caseId}/remote-jobs`;
  const [pending,setPending] = useState<Pending | null>(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null');
      return value && typeof value.key === 'string' && typeof value.body?.profile === 'string' ? value : null;
    } catch { return null; }
  });
  const [profiles,setProfiles] = useState<Profile[] | null>(null);
  const [rows,setRows] = useState<Run[]>([]);
  const [selection,setSelection] = useState(() => readCaseSelection(userId,caseId).remoteRunId ?? '');
  const [profileName,setProfileName] = useState('');
  const [script,setScript] = useState('');
  const [inputs,setInputs] = useState('');
  const [outputs,setOutputs] = useState('');
  const [seconds,setSeconds] = useState(600);
  const [checkpoint,setCheckpoint] = useState('');
  const [mappings,setMappings] = useState('');
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [retry,setRetry] = useState(0);
  const live = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const profile = profiles?.find(candidate => candidate.name === profileName) ?? profiles?.[0];
  const selected = rows.find(row => row.id === selection);

  function select(id: string) {
    saveCaseSelection(userId,caseId,{ remoteRunId:id });
    setSelection(id);
  }

  function accept(row: Run) {
    if (row.case_id !== caseId) throw new Error('远程作业回执的算例归属无法核对。');
    setRows(previous => [row,...previous.filter(item => item.id!==row.id)]);
  }

  useEffect(() => {
    const controller = new AbortController();
    live.current = controller;
    setError('');
    const options = { signal:controller.signal };
    void Promise.all([jobRecoveryRequest<Profile[]>('/remote/profiles',options),jobRecoveryRequest<Run[]>(endpoint,options)])
      .then(([available, history]) => {
        if (controller.signal.aborted) return;
        setProfiles(available); setRows(history.filter(run => run.case_id === caseId));
      }).catch(failure => { if (!controller.signal.aborted) setError(jobRecoveryError(failure)); });
    return () => controller.abort();
  }, [caseId,endpoint,retry]);

  async function action(work: (signal: AbortSignal) => Promise<void>, report = true) {
    const signal = live.current?.signal;
    if (!signal || signal.aborted || inFlight.current) return;
    inFlight.current = true; setBusy(true);
    if (report) setError('');
    try { await work(signal); }
    catch (failure) { if (!signal.aborted) setError(jobRecoveryError(failure)); }
    finally { inFlight.current = false; if (!signal.aborted) setBusy(false); }
  }

  const active = !!selected && !terminal.has(selected.state);
  useEffect(() => {
    if (!selection || !active || readonly) return;
    const timer = window.setInterval(() => { void refresh(selection, 'refresh', false); }, 3200);
    return () => window.clearInterval(timer);
  }, [selection,active,readonly]);

  async function refresh(id: string, command: 'refresh' | 'cancel', report = true) {
    await action(async signal => {
      const row = await jobRecoveryRequest<Run>(`${endpoint}/${id}:${command}`, { signal, body:{} });
      if (row.id !== id) throw new Error('远程作业回执与所选作业不一致。');
      if (!signal.aborted) accept(row);
    }, report);
  }

  async function submit() {
    await action(async signal => {
      let request = pending;
      if (!request) {
        if (!profile || !script.trim()) throw new Error('请选择集群并填写已有的作业脚本路径。');
        const input_paths = Array.from(new Set([script.trim(),...lines(inputs)]));
        const body: Submit = { profile:profile.name, script:script.trim(), input_paths,
          output_paths:lines(outputs), timeout_seconds:Math.min(profile.max_seconds,seconds) };
        if (checkpoint) {
          const files = lines(mappings).map(line => {
            const [source,target,...extra] = line.split('=>').map(path => path.trim());
            if (!source || extra.length || target === '') throw new Error('请核对续算文件的源路径和目标路径。');
            return { output_path:source, input_path:target ?? source };
          });
          if (!files.length) throw new Error('请选择要从历史作业取回的检查点文件。');
          body.restart = { run_id:checkpoint, files };
        }
        request = { key:crypto.randomUUID(), body };
        sessionStorage.setItem(storageKey, JSON.stringify(request));
        setPending(request);
      }
      const row = await jobRecoveryRequest<Run>(endpoint, { signal, body:request.body, key:request.key });
      if (signal.aborted) return;
      if (row.profile!==request.body.profile || row.script!==request.body.script) throw new Error('远程提交回执无法核对。');
      accept(row); select(row.id);
      if (row.scheduler_id || terminal.has(row.state)) { sessionStorage.removeItem(storageKey); setPending(null); }
    });
  }

  async function download(run: Run, path: string, receipt: FileReceipt) {
    await action(async signal => {
      const token = localStorage.getItem('vf_token');
      const response = await fetch(`/api/v1${endpoint}/${run.id}/files?path=${encodeURIComponent(path)}`,
        { signal, headers:token ? { Authorization:`Bearer ${token}` } : {}, credentials:'same-origin' });
      if (!response.ok) throw new Error('成果暂时无法下载，请刷新后重试。');
      const data = await response.arrayBuffer();
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',data)),
        octet => octet.toString(16).padStart(2,'0')).join('');
      if (receipt.size!==data.byteLength || receipt.sha256!==hash) throw new Error('成果摘要与原作业不一致，请重新读取。');
      if (!signal.aborted) saveDerivative(data,path);
    });
  }

  return <section className="remote-jobs execution-launch" aria-label="远程集群作业">
    <h2>远程集群</h2>
    {error && <p role="alert">{error}</p>}
    {profiles===null && (error ? <button onClick={() => setRetry(value => value+1)}>重新读取</button>
      : <p role="status">正在读取集群配置…</p>)}
    {profiles?.length===0 && <p className="execution-muted">当前账户没有可用的集群配置。</p>}
    {profile && !readonly && <details className="remote-hpc-form" open={pending ? true : undefined}>
      <summary>新建远程作业</summary>
      {pending ? <div className="execution-actions"><p role="status">待确认 · {pending.body.profile} · {pending.body.script}</p>
        <button type="button" onClick={() => void submit()} disabled={busy}>核对原提交</button></div>
        : <form onSubmit={event => { event.preventDefault(); void submit(); }}>
          <fieldset disabled={busy}>
            <label>集群<select value={profile.name} onChange={event => { setProfileName(event.target.value); setCheckpoint(''); }}>
              {profiles?.map(item => <option key={item.name} value={item.name}>{item.name} · {item.scheduler==='slurm' ? 'Slurm' : 'PBS'}</option>)}
            </select></label>
            <label>墙钟上限（秒）<input type="number" min={60} max={profile.max_seconds}
              value={Math.min(seconds,profile.max_seconds)} onChange={event => setSeconds(event.target.valueAsNumber||60)} required /></label>
            <label className="execution-wide">脚本路径<input value={script} onChange={event => setScript(event.target.value)} required /></label>
            <label>其他输入路径（每行一个）<textarea value={inputs} onChange={event => setInputs(event.target.value)} rows={3} /></label>
            <label>成果路径（每行一个）<textarea value={outputs} onChange={event => setOutputs(event.target.value)} rows={3} /></label>
            <label className="execution-wide">从历史成果续算<select value={checkpoint} onChange={event => setCheckpoint(event.target.value)}>
              <option value="">直接使用本次输入</option>
              {rows.filter(row => row.profile===profile.name && ['succeeded','cancelled'].includes(row.state)
                && Object.keys(row.observation.results ?? {}).length>0).map(row =>
                <option key={row.id} value={row.id}>{row.script} · {row.scheduler_id}</option>)}
            </select></label>
            {checkpoint && <label className="execution-wide">检查点路径（每行一个，改名可用“源路径 =&gt; 目标路径”）
              <textarea value={mappings} onChange={event => setMappings(event.target.value)} rows={3} required /></label>}
          </fieldset>
          <button className="execution-primary" type="submit" disabled={busy}>提交到集群</button>
        </form>}
    </details>}
    <div className="remote-hpc-history" aria-label="远程作业历史">
      {profiles && !rows.length && <p className="execution-muted">这个算例还没有远程作业。</p>}
      {rows.map(row => <button type="button" key={row.id} aria-pressed={selection===row.id}
        onClick={() => select(row.id)}><span>{row.script}</span><span>{labels[row.state] ?? row.state}</span></button>)}
    </div>
    {selected && <article className="remote-hpc-result" aria-label="远程作业状态">
      <h3>{selected.script}</h3>
      <p role="status">{labels[selected.state] ?? selected.state} · {selected.profile}{selected.scheduler_id && ` · #${selected.scheduler_id}`}</p>
      {selected.observation.reason==='missing_results' && <p role="alert">调度作业已结束，声明的成果文件有缺失。</p>}
      {active && !readonly && <div className="execution-actions">
        <button type="button" disabled={busy} onClick={() => void refresh(selected.id,'refresh')}>刷新状态</button>
        <button type="button" disabled={busy || selected.state==='cancelling'} onClick={() => void refresh(selected.id,'cancel')}>取消作业</button>
      </div>}
      {Object.entries(selected.observation.results ?? {}).map(([path,receipt]) => <button type="button"
        key={path} disabled={busy} onClick={() => void download(selected,path,receipt)}>下载 {path}</button>)}
    </article>}
  </section>;
}
