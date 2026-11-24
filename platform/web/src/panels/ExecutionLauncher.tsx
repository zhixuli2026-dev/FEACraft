import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';
import { launchLabel, launchPath, launchPayload, launchStorageKey, nativeSolvers, postprocessScriptCommand, readLaunch, saveLaunch, validateLaunch, verifyLaunchReceipt } from '../utils/executionLaunch';
import type { LaunchLookup, LaunchMode, LaunchReceipt, LaunchRequest, LaunchRuntime, LaunchTarget, NativeSolver, PreparedBatchOperation, SingleJobType } from '../utils/executionLaunch';
import type { CaseDictSnapshot } from '../utils/useCaseDraft';
import { workflowTargetLabel } from '../utils/workflowRecovery';
import PressureTransferFields, { emptyPressureTransfer } from './PressureTransferFields';
import './ExecutionLauncher.css';

const launchError = (failure: unknown) => failure instanceof Error && !(failure instanceof ApiError) && !(failure instanceof TypeError)
  ? failure.message : jobRecoveryError(failure);
const solveLabels = { serial: '串行', mpi: 'MPI 并行', udf: 'UDF' } as const;
type BatchMemberDraft = { id: number; kind: PreparedBatchOperation['kind']; directory: string; solver: string; ranks: number; name: string };
const batchMember = (id: number): BatchMemberDraft => ({ id, kind: 'serial', directory: '', solver: 'icoFoam', ranks: 2, name: 'myFunctionObject' });
const controlDictionary = '/system/controlDict';
const runtimeLabel = { windows: 'Windows cmd', wsl: 'WSL vibeflow-linux', 'wsl-sandbox': 'WSL 隔离沙箱' };

export default function ExecutionLauncher({ userId, caseId, caseName, onAccepted }: {
  userId: number; caseId: number; caseName: string; onAccepted: (receipt: LaunchReceipt) => void;
}) {
  const readOnly = useStore(s => s.currentWs?.access_role === 'viewer');
  const fileTree = useStore(s => s.fileTree);
  const [restored] = useState(() => { try { return { request: readLaunch(userId, caseId), error: '' }; }
    catch { return { request: null, error: '本机提交记录无法读取，请保留记录并核对任务历史。' }; } });
  const [open, setOpen] = useState(!!restored.request || !!restored.error);
  const originalNative = restored.request?.mode === 'native' ? restored.request.body : undefined;
  const [mode, setMode] = useState<LaunchMode>(originalNative ? 'native' : 'single');
  const [solver, setSolver] = useState<NativeSolver>(originalNative?.solver ?? 'calculix');
  const [inputPath, setInputPath] = useState(originalNative?.input_path ?? '');
  const [restartPublication, setRestartPublication] = useState(originalNative?.restart?.publication_id ?? '');
  const [checkpoint, setCheckpoint] = useState(originalNative?.restart?.checkpoint_path ?? '');
  const [usePressure, setUsePressure] = useState(!!originalNative?.pressure_transfer);
  const [pressure, setPressure] = useState(originalNative?.pressure_transfer ?? emptyPressureTransfer);
  const [runtime, setRuntime] = useState<LaunchRuntime>('windows');
  const [jobType, setJobType] = useState<SingleJobType>('shell');
  const script = mode === 'single' && jobType === 'convert';
  const [commands, setCommands] = useState('');
  const [batchType, setBatchType] = useState<'commands' | 'operations'>('commands');
  const [members, setMembers] = useState(() => [batchMember(0)]);
  const nextMemberId = useRef(1);
  const [limit, setLimit] = useState(2);
  const [runDir, setRunDir] = useState('C-run/case-1');
  const [target, setTarget] = useState<LaunchTarget>('geometry');
  const [geometryMode, setGeometryMode] = useState<'preview' | 'export'>('preview');
  const [solveMode, setSolveMode] = useState<'case' | 'serial' | 'mpi' | 'udf'>('case');
  const [subdomains, setSubdomains] = useState(2), [udfName, setUdfName] = useState('myFunctionObject');
  const [reportMode, setReportMode] = useState<'compose' | 'markdown'>('compose');
  const [timeout, setTimeoutSeconds] = useState(86400), [logKiB, setLogKiB] = useState(16384);
  const [review, setReview] = useState<LaunchRequest | null>(null);
  const [pending, setPending] = useState(restored.request);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [canRevise, setCanRevise] = useState(false);
  const live = useRef(true), acting = useRef(false), rejected = useRef(false);
  const directoryEdited = useRef(false);
  const feedback = useRef<HTMLParagraphElement>(null);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const current = () => live.current && useStore.getState().user?.id === userId && useStore.getState().currentCase?.id === caseId;
  const disabled = busy || readOnly || !!restored.error;
  const prepared = mode === 'batch' && batchType === 'operations';
  const native = nativeSolvers[solver];
  const nativeInputs = mode === 'native' ? fileTree.filter(entry => entry.kind !== 'directory'
    && entry.path.toLowerCase().endsWith(native.suffix)).map(entry => entry.path).sort() : [];
  const runDirectories = prepared ? fileTree.filter(entry => entry.path.startsWith('C-run/') && entry.path.endsWith(controlDictionary))
    .map(entry => entry.path.slice(0, -controlDictionary.length)).sort() : [];

  function updateMember(id: number, changes: Partial<BatchMemberDraft>) {
    setMembers(entries => entries.map(member => member.id === id ? { ...member, ...changes } : member));
    setError('');
  }

  function accept(request: LaunchRequest, raw: unknown) {
    const receipt = verifyLaunchReceipt(request, raw);
    try { sessionStorage.removeItem(launchStorageKey(userId, caseId)); }
    catch { if (current()) setNotice('已找到原任务，本机确认记录尚未清除；再次读取仍会关联同一任务。'); }
    if (current()) {
      setPending(null); setReview(null); setOpen(false); setCommands('');
      if (request.mode === 'batch' && request.body.operations) setMembers([batchMember(0)]);
      if (request.mode === 'native') { setRestartPublication(''); setCheckpoint(''); setUsePressure(false); setPressure(emptyPressureTransfer); }
      onAccepted(receipt);
    }
  }
  async function inspect(request: LaunchRequest) {
    const value = await jobRecoveryRequest<LaunchLookup>(`/cases/${caseId}/execution-submission`,
      { body: { kind: request.mode, key: request.key, body: request.body } });
    if (value.case_id !== caseId || value.kind !== request.mode || typeof value.found !== 'boolean') throw new Error('原提交查询回执无法核验。');
    if (value.found) { accept(request, value); return true; }
    if (current()) {
      setNotice('暂未找到原任务，提交记录继续保留。可重试原提交；尚未创建新请求。');
      setCanRevise(rejected.current);
    }
    return false;
  }
  async function prepare() {
    if (disabled || acting.current || pending || review) return;
    acting.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const common = { schema: 1 as const, userId, caseId, key: crypto.randomUUID() };
      const budget = { timeout_seconds: timeout, max_log_bytes: logKiB * 1024 };
      let directory = runDir.trim();
      let snapshot: CaseDictSnapshot | null = null;
      if (mode === 'workflow' || (mode === 'single' && jobType === 'solve')) {
        snapshot = await jobRecoveryRequest<CaseDictSnapshot>(`/cases/${caseId}/caseDict`);
        const configured = snapshot.parsed?.postProcessing?.runDir;
        if (!directoryEdited.current && typeof configured === 'string' && configured) {
          directory = configured;
          if (current()) setRunDir(directory);
        }
      }
      let request: LaunchRequest;
      if (mode === 'single') request = { ...common, mode, body: { type: jobType,
        command: script ? postprocessScriptCommand : commands.trim(), runtime: script ? 'wsl-sandbox' : runtime, ...budget,
        ...(jobType === 'solve' ? { run_dir: directory } : {}) } };
      else if (mode === 'batch') {
        if (prepared) {
          const operations = members.map((member): PreparedBatchOperation => {
            const inputs = { ...budget, run_dir: member.directory };
            if (member.kind === 'udf') return { ...inputs, kind: 'udf', name: member.name.trim() };
            const solver = member.solver.trim();
            return member.kind === 'mpi' ? { ...inputs, kind: 'mpi', solver, subdomains: member.ranks }
              : { ...inputs, kind: 'serial', solver };
          });
          request = { ...common, mode, body: { operations, max_concurrent: limit } };
        } else {
          request = { ...common, mode, body: { commands: commands.split('\n').map(c => c.trim()).filter(Boolean), runtime, max_concurrent: limit } };
        }
      }
      else if (mode === 'native') {
        const publication_id = restartPublication.trim().toLowerCase(), checkpoint_path = checkpoint.trim();
        request = { ...common, mode, body: { solver, input_path: inputPath,
          ...(solver === 'calculix' && usePressure ? { pressure_transfer: { ...pressure, node_set: pressure.node_set.trim() } } : {}),
          ...(native.restart && (publication_id || checkpoint_path) ? { restart: { publication_id, checkpoint_path } } : {}) } };
      } else {
        if (!snapshot || snapshot.parse_status !== 'ok') throw new Error('caseDict尚未通过解析，请在算例面板修复后提交工作流。');
        const solves = ['solve', 'postprocess', 'report'].includes(target);
        if (solves && solveMode === 'case' && snapshot.workflow_defaults_error) throw new Error(snapshot.workflow_defaults_error);
        const execution = solveMode === 'case' ? {solve_mode: 'serial' as const, ...snapshot.workflow_defaults}
          : {solve_mode: solveMode, ...(solveMode === 'mpi' ? {subdomains} : solveMode === 'udf' ? {udf_name: udfName.trim()} : {})};
        request = { ...common, mode, body: { target, expected_revision: snapshot.dict_revision, run_dir: directory, budget,
          parameters: { geometry_mode: geometryMode, ...(solves ? execution : {}),
            ...(solves && snapshot.workflow_defaults?.placement === 'remote' ? { placement: 'remote' } : {}),
            ...(target === 'report' ? { report_mode: reportMode } : {}) } } };
      }
      validateLaunch(request, userId, caseId);
      if (current()) setReview(request);
    } catch (failure) { if (current()) setError(launchError(failure)); }
    finally { acting.current = false; if (current()) { setBusy(false); feedback.current?.focus(); } }
  }
  async function send() {
    if (disabled || acting.current || (!pending && !review)) return;
    const request = pending ?? review!;
    acting.current = true; setBusy(true); setError(''); setNotice(''); setCanRevise(false); rejected.current = false;
    try {
      try { saveLaunch(request); }
      catch { throw new Error('无法保存本次确认记录，尚未发送请求。请允许浏览器会话存储后重试。'); }
      if (current()) { setPending(request); setReview(null); }
      try {
        const value = await jobRecoveryRequest<unknown>(launchPath(request), { body: launchPayload(request), key: request.key });
        accept(request, value);
      } catch (failure) {
        // A workflow may already exist when dispatch fails. A 4xx alone is
        // never proof of non-admission; look up the original key before editing.
        rejected.current = failure instanceof ApiError && [400, 422].includes(failure.status);
        if (current()) setError(`提交尚未确认：${launchError(failure)}`);
        try { await inspect(request); } catch { if (current()) setNotice('原结果暂时无法读取，确认记录已保留。请重试读取或重试原提交。'); }
      }
    } catch (failure) { if (current()) setError(launchError(failure)); }
    finally { acting.current = false; if (current()) { setBusy(false); feedback.current?.focus(); } }
  }
  async function readOriginal() {
    if (!pending || acting.current) return;
    acting.current = true; setBusy(true); setError('');
    try { await inspect(pending); } catch (failure) { if (current()) setError(launchError(failure)); }
    finally { acting.current = false; if (current()) { setBusy(false); feedback.current?.focus(); } }
  }
  function revise() {
    if (!canRevise || !pending || acting.current) return;
    try { sessionStorage.removeItem(launchStorageKey(userId, caseId)); setPending(null); setCanRevise(false); setError(''); setNotice('已核对本次拒绝且未找到任务，请修改参数后重新核对。'); }
    catch { setError('本机确认记录尚未清除，请稍后重试。'); }
  }
  const confirmed = pending ?? review;
  return <details className="execution-launch" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>新建任务{pending ? ' · 原提交待核对' : ''}</summary>
    {open && <section aria-label="新建任务" aria-busy={busy}>
      <p>算例：{caseName}</p>
      {readOnly && <p role="status">当前工作区为只读，可以查看任务；提交需要写权限。</p>}
      {restored.error && <p role="alert">{restored.error}</p>}
      {!confirmed && <fieldset disabled={disabled}>
        <label>任务方式<select aria-label="任务方式" value={mode} onChange={e => { setMode(e.target.value as LaunchMode); setError(''); }}>
          <option value="single">单作业</option><option value="batch">批次</option><option value="workflow">工作流</option><option value="native">开源求解</option>
        </select></label>
        {mode === 'batch' && <label>批次类型<select value={batchType} onChange={e => { setBatchType(e.target.value as typeof batchType); setError(''); }}>
          <option value="commands">命令列表</option><option value="operations">多工况求解</option>
        </select></label>}
        {mode !== 'workflow' && mode !== 'native' && !prepared && !script && <label>运行环境<select aria-label="运行环境" value={runtime} onChange={e => setRuntime(e.target.value as LaunchRuntime)}>
          <option value="windows">Windows cmd</option><option value="wsl">WSL vibeflow-linux</option><option value="wsl-sandbox">WSL 隔离沙箱</option>
        </select></label>}
        {mode === 'single' && <label>作业类型<select aria-label="作业类型" value={jobType} onChange={e => { setJobType(e.target.value as SingleJobType); setError(''); }}>
          <option value="shell">普通命令</option><option value="solve">OpenFOAM 求解</option><option value="convert">后处理脚本</option>
        </select></label>}
        {script && <p className="execution-wide execution-muted">在 WSL 隔离沙箱的算例根目录运行 <code>D-post/postprocess.py</code>。</p>}
        {mode !== 'workflow' && mode !== 'native' && !prepared && !script && <label className="execution-wide">{mode === 'batch' ? '批量命令（每行一项）' : '运行命令'}
          <textarea aria-label={mode === 'batch' ? '批量命令' : '运行命令'} rows={3} maxLength={mode === 'batch' ? 200050 : 4000} value={commands} onChange={e => setCommands(e.target.value)} />
          {mode === 'batch' && <span className="execution-muted">最多50项，每项最多4000字符。每项保留独立作业、日志和恢复入口。</span>}
        </label>}
        {mode === 'native' && <>
          <label>开源求解器<select value={solver} onChange={event => {
            setSolver(event.target.value as NativeSolver); setInputPath(''); setRestartPublication(''); setCheckpoint(''); setUsePressure(false); setPressure(emptyPressureTransfer); setError('');
          }}>
            {(Object.keys(nativeSolvers) as NativeSolver[]).map(key => <option key={key} value={key}>{nativeSolvers[key].label}</option>)}
          </select></label>
          <label className="execution-wide">求解输入文件<select value={nativeInputs.includes(inputPath) ? inputPath : ''}
            onChange={event => { setInputPath(event.target.value); setError(''); }} disabled={!nativeInputs.length}>
            <option value="">选择 {native.suffix} 文件</option>
            {nativeInputs.map(path => <option key={path} value={path}>{path}</option>)}
          </select></label>
          {!nativeInputs.length && <p className="execution-wide execution-muted" role="status">请先在算例文件中添加 {native.suffix} 输入及其依赖。</p>}
          {solver === 'calculix' && nativeInputs.includes(inputPath) && <>
            <label className="execution-wide">载荷来源<select value={usePressure ? 'pressure' : 'deck'}
              onChange={event => { setUsePressure(event.target.value === 'pressure'); setError(''); }}>
              <option value="deck">算例载荷</option><option value="pressure">OpenFOAM 压力合力</option>
            </select></label>
            {usePressure && <PressureTransferFields caseId={caseId} value={pressure} onChange={setPressure} />}
          </>}
          {native.restart && <details key={solver} className="execution-wide" open={originalNative?.solver === solver && !!originalNative.restart || undefined}>
            <summary>从历史发布续算</summary>
            <div className="execution-resource-fields">
              <label>发布 ID<input value={restartPublication} maxLength={36} onChange={event => setRestartPublication(event.target.value)} /></label>
              <label>续算文件路径<input value={checkpoint} maxLength={1024} onChange={event => setCheckpoint(event.target.value)} /></label>
              <p className="execution-wide execution-muted">在历史作业的求解成果中查看发布 ID 与 {native.restart} 文件路径。</p>
            </div>
          </details>}
        </>}
        {prepared && <div className="execution-wide execution-batch-members">
          {runDirectories.length === 0 && <p role="status" className="execution-muted">当前算例没有已准备的 C-run 目录。请先在算例面板准备基准工况或派生已有工况。</p>}
          {members.map((member, index) => <fieldset className="execution-batch-member" key={member.id}>
            <legend>工况 {index + 1}</legend>
            <label className="execution-wide">求解目录<select value={member.directory} onChange={e => updateMember(member.id, { directory: e.target.value })}>
              <option value="">选择 C-run 目录</option>
              {member.directory && !runDirectories.includes(member.directory) && <option value={member.directory} disabled>{member.directory}（已不可用）</option>}
              {runDirectories.map(path => <option key={path} value={path}>{path}</option>)}
            </select></label>
            <label>求解方式<select value={member.kind} onChange={e => updateMember(member.id, { kind: e.target.value as BatchMemberDraft['kind'] })}>
              <option value="serial">{solveLabels.serial}</option><option value="mpi">{solveLabels.mpi}</option><option value="udf">{solveLabels.udf}</option>
            </select></label>
            {member.kind === 'udf' ? <label>UDF 名称<input maxLength={40} value={member.name} onChange={e => updateMember(member.id, { name: e.target.value })} /></label>
              : <label>求解器<input value={member.solver} onChange={e => updateMember(member.id, { solver: e.target.value })} /></label>}
            {member.kind === 'mpi' && <label>MPI 子域数<input type="number" min={1} max={64} value={member.ranks} onChange={e => updateMember(member.id, { ranks: Number(e.target.value) })} /></label>}
            {members.length > 1 && <button type="button" aria-label={`移除工况 ${index + 1}`} onClick={() => {
              setMembers(entries => entries.filter(entry => entry.id !== member.id)); setError('');
            }}>移除</button>}
          </fieldset>)}
          <button type="button" disabled={members.length >= 50 || !runDirectories.length} onClick={() => {
            const member = batchMember(nextMemberId.current++);
            setMembers(entries => [...entries, member]); setError('');
          }}>添加工况</button>
        </div>}
        {mode === 'batch' && <label>并发上限<input aria-label="批量并发" type="number" min={1} max={4} value={limit} onChange={e => setLimit(Number(e.target.value))} /></label>}
        {mode === 'workflow' && <>
          <label>目标产物<select aria-label="工作流目标" value={target} onChange={e => setTarget(e.target.value as LaunchTarget)}>
            {(['geometry', 'mesh', 'solve', 'postprocess', 'report'] as const).map(value => <option key={value} value={value}>{workflowTargetLabel(value)}</option>)}
          </select></label>
          <label>几何方式<select aria-label="几何方式" value={geometryMode} onChange={e => setGeometryMode(e.target.value as 'preview' | 'export')}><option value="preview">预览</option><option value="export">导出STEP/STL</option></select></label>
          {['solve', 'postprocess', 'report'].includes(target) && <label>求解方式<select aria-label="求解方式" value={solveMode} onChange={e => setSolveMode(e.target.value as typeof solveMode)}><option value="case">按算例设置</option><option value="serial">串行</option><option value="mpi">MPI并行</option><option value="udf">UDF</option></select></label>}
          {['solve', 'postprocess', 'report'].includes(target) && solveMode === 'mpi' && <label>子域数<input aria-label="MPI子域数" type="number" min={1} max={64} value={subdomains} onChange={e => setSubdomains(Number(e.target.value))} /></label>}
          {['solve', 'postprocess', 'report'].includes(target) && solveMode === 'udf' && <label>UDF名称<input aria-label="UDF名称" value={udfName} onChange={e => setUdfName(e.target.value)} /></label>}
          {target === 'report' && <label>报告方式<select aria-label="报告方式" value={reportMode} onChange={e => setReportMode(e.target.value as typeof reportMode)}><option value="compose">组织报告内容</option><option value="markdown">已有Markdown</option></select></label>}
          <p className="execution-wide execution-muted">从当前阶段执行至目标产物；阶段确认仍需逐步核对。已有阶段的重算请使用原流程的恢复入口。</p>
        </>}
        {(mode === 'workflow' || (mode === 'single' && jobType === 'solve')) && <label className="execution-wide">求解目录<input aria-label="求解目录" value={runDir} onChange={e => { directoryEdited.current = true; setRunDir(e.target.value); }} /><span className="execution-muted">单独求解需先有网格；填写命令实际使用的C-run子目录。</span></label>}
        {(mode === 'single' || mode === 'workflow' || prepared) && <details className="execution-wide"><summary>{prepared ? '每项资源限制' : '资源限制'}</summary><div className="execution-resource-fields">
          <label>期限（秒）<input aria-label="执行期限秒" type="number" min={1} max={86400} value={timeout} onChange={e => setTimeoutSeconds(Number(e.target.value))} /></label>
          <label>日志上限（KiB）<input aria-label="日志上限KiB" type="number" min={1} max={65536} value={logKiB} onChange={e => setLogKiB(Number(e.target.value))} /></label>
        </div></details>}
        <button className="execution-primary" type="button" disabled={(prepared && members.some(member => !runDirectories.includes(member.directory)))
          || (mode === 'native' && !nativeInputs.includes(inputPath))} onClick={() => void prepare()}>核对提交</button>
      </fieldset>}
      {confirmed && <div className="execution-review">
        <h3>{pending ? '核对原提交' : '确认任务'}</h3>
        <p>{confirmed.mode === 'batch' ? confirmed.body.operations ? '多工况求解' : '批量命令' : launchLabel(confirmed.mode)} · {caseName}</p>
        {confirmed.mode === 'workflow' ? <p>{workflowTargetLabel(confirmed.body.target)} · 输入修订{confirmed.body.expected_revision} · {confirmed.body.run_dir}</p>
          : confirmed.mode === 'native' ? <>
            <p>{nativeSolvers[confirmed.body.solver].label} · {confirmed.body.input_path}</p>
            {confirmed.body.restart && <p>续算 · {confirmed.body.restart.checkpoint_path} · {confirmed.body.restart.publication_id}</p>}
            {confirmed.body.pressure_transfer && <p>{confirmed.body.pressure_transfer.distribution === 'conformalNodal' ? '节点压力载荷' : '压力合力'} · {confirmed.body.pressure_transfer.patch} · {confirmed.body.pressure_transfer.time} s
              → {confirmed.body.pressure_transfer.node_set} · 发布 {confirmed.body.pressure_transfer.publication_id}</p>}
          </>
          : confirmed.mode === 'batch' && confirmed.body.operations ? <>
            <p>{confirmed.body.operations.length} 项 · 并发上限 {confirmed.body.max_concurrent}</p>
            <ol className="execution-batch-review">{confirmed.body.operations.map(operation => <li key={operation.run_dir}>
              {operation.run_dir} · {solveLabels[operation.kind]} · {operation.kind === 'udf' ? operation.name : operation.solver}
              {operation.kind === 'mpi' && ` · ${operation.subdomains} 个子域`}
              <span className="execution-muted"> · {operation.timeout_seconds} 秒 · {operation.max_log_bytes / 1024} KiB 日志</span>
            </li>)}</ol></>
            : <><p>{confirmed.body.runtime && runtimeLabel[confirmed.body.runtime]}
              {confirmed.mode === 'batch' && confirmed.body.commands && ` · ${confirmed.body.commands.length} 项 · 并发上限 ${confirmed.body.max_concurrent}`}</p>
              <pre>{confirmed.mode === 'single' ? confirmed.body.command : confirmed.body.commands?.join('\n')}</pre></>}
        {confirmed.mode === 'single' && confirmed.body.run_dir && <p>求解目录：{confirmed.body.run_dir}</p>}
        {confirmed.mode === 'workflow' && <p>几何：{confirmed.body.parameters.geometry_mode === 'preview' ? '预览' : '导出STEP/STL'}
          {confirmed.body.parameters.solve_mode && ` · 求解：${({ serial: '串行', mpi: 'MPI并行', udf: 'UDF' })[confirmed.body.parameters.solve_mode]}`}
          {confirmed.body.parameters.subdomains && ` · ${confirmed.body.parameters.subdomains}个子域`}
          {confirmed.body.parameters.udf_name && ` · ${confirmed.body.parameters.udf_name}`}
          {confirmed.body.parameters.placement === 'remote' && ' · 远程 worker'}
          {confirmed.body.parameters.report_mode && ` · 报告：${confirmed.body.parameters.report_mode === 'compose' ? '组织报告内容' : '已有Markdown'}`}</p>}
        {(confirmed.mode === 'single' || confirmed.mode === 'workflow') && <p>期限：{confirmed.mode === 'single' ? confirmed.body.timeout_seconds : confirmed.body.budget.timeout_seconds}秒
          · 日志上限：{(confirmed.mode === 'single' ? confirmed.body.max_log_bytes : confirmed.body.budget.max_log_bytes) / 1024} KiB</p>}
        {pending ? <><p>原提交已保留。读取结果只查询任务；重试会沿用原请求，不另建一份。</p>
          <div className="execution-actions"><button type="button" disabled={busy} onClick={() => void readOriginal()}>读取原提交结果</button>
            <button type="button" disabled={disabled} onClick={() => void send()}>重试原提交</button>
            {canRevise && <button type="button" disabled={disabled} onClick={revise}>修改已拒绝请求</button>}</div></>
          : <div className="execution-actions"><button type="button" className="execution-primary" disabled={disabled} onClick={() => void send()}>确认提交</button>
            <button type="button" disabled={busy} onClick={() => setReview(null)}>返回修改</button></div>}
      </div>}
      <p ref={feedback} tabIndex={-1} className="execution-feedback" role={error ? 'alert' : 'status'}>{busy ? '正在核对，请稍候…' : error || notice}</p>
    </section>}
  </details>;
}
