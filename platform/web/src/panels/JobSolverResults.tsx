import { Fragment, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { nativeSolvers, type NativeSolver } from '../utils/executionLaunch';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import { saveDerivative } from '../utils/reportDerivative';
import './JobSolverResults.css';

type Artifact = { size: number; sha256: string };
type Electrode = { name: string; tag: number; area_m2: number; value_si: number; native_value: number }
  & ({ kind: 'voltage' | 'current' } | { kind: 'impedance'; external_potential_v: number });
type NumericalRun = {
  linear_solver?: string; linear?: { method: string }; options?: { LINEAR_SOLVER: string };
  executioner?: { solve_type: string; petsc_options_value: string };
  observed: { algorithm?: string; converged_increments?: number; maximum_iterations?: number; period?: number;
    maximum_linear_iterations?: number; maximum_nonlinear_iterations?: number; steps?: number; adaptive_trials?: number;
    last_time?: number; time_steps?: number; time_step_s?: number; final_log_density_residual?: number;
    density_target_reached?: boolean; minimum_cfl?: number; maximum_cfl?: number; physical_cfl?: number;
    minimum_time_step?: number; maximum_time_step?: number };
  native_input_sha256?: string;
};
type Trace = {
  publication: { id: string; job_id: number; attempt_id: string; kind: string; generation: number; active: boolean };
  files: Record<string, Artifact>;
  contact?: { interfaces: { name: string; dependent_surface: string; independent_surface: string; normal_stiffness_pa_per_m: number }[] };
  pressure_transfer?: { publication_id: string; surface: string; patch: string; time: number; node_set: string;
    force_n: [number, number, number]; source_area_m2: number; source_field?: 'p' | 'pa';
    distribution?: 'uniformResultant' | 'conformalNodal'; nodal_loads?: { positions_m: number[][] } };
  numerical?: NumericalRun;
  element_families?: { declared: string[]; native_types: string[] };
  mesh_adaptivity?: { mode: 'h' | 'p' | 'hp' | 'goalOriented'; native_mode?: 'h' | 'p' | 'hp'; cycles: number;
    goal?: { postprocessor: string; target: number; absolute_tolerance: number };
    observed: { cells: number[]; dofs: number[]; goal?: { first_met_cycle: number; final_absolute_error: number } } };
  electrodes?: { values: Electrode[]; native_input_sha256: string };
  coupling?: { mode: 'jouleHeating'; current_solver: number; heat_solver: number; body?: number; native_input_sha256: string;
    bodies?: { body: number; conductivity_s_m: number }[];
    electrical_interfaces?: { name: string; regions: [string, string]; boundary_tag: number; shared_faces: number }[] };
  electrostatic?: { material: string; native_material: number; relative_permittivity: number;
    vacuum_permittivity_f_m: number; native_input_sha256: string };
};
const quantity = new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 7 });
const scientific = new Intl.NumberFormat('zh-CN', { notation: 'scientific', maximumSignificantDigits: 7 });
const electrodeQuantity = {
  voltage: { label: '施加电压', unit: 'V' },
  current: { label: '施加总电流', unit: 'A' },
  impedance: { label: '接触电阻', unit: 'Ω' },
};
const previewLimit = 128 * 1024;
const canPreview = (path: string, solver: NativeSolver) => /\.(csv|txt|log|mess|sta)$/i.test(path)
  || (solver === 'calculix' && /\.dat$/i.test(path));
function readError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return '请重新登录后读取求解成果。';
    if (error.status === 403 || error.status === 404) return '当前账户无法读取此求解成果，请核对发布记录与算例权限。';
    if (error.code === 'evidence_invalid') return '所选求解成果无法核验。';
  }
  if (error instanceof Error && !(error instanceof TypeError) && !(error instanceof ApiError)) return error.message;
  return '求解成果读取失败，请重试。';
}

function NumericalEvidence({ run }: { run: NumericalRun }) {
  const observed = run.observed;
  const method = run.linear?.method ?? run.options?.LINEAR_SOLVER ?? run.linear_solver
    ?? (run.executioner?.petsc_options_value === "'lu'" ? 'PETSc · LU' : undefined);
  const algorithm = method ? ({ spooles: 'SPOOLES', iterativeScaling: '迭代法 · 对角缩放',
    iterativeCholesky: '迭代法 · Cholesky' } as Record<string, string>)[method] ?? method : undefined;
  return <div role="region" aria-label="数值求解证据" className="job-solver-setup">
    <strong>数值求解证据</strong>
    <dl>
      {algorithm && <><dt>线性求解器</dt><dd>{algorithm}</dd></>}
      {(observed.algorithm ?? run.executioner?.solve_type) && <><dt>非线性算法</dt>
        <dd>{observed.algorithm ?? run.executioner?.solve_type}</dd></>}
      {observed.converged_increments !== undefined && <><dt>收敛增量</dt><dd>{observed.converged_increments} · 模型时间 {observed.period}</dd></>}
      {observed.maximum_linear_iterations !== undefined && <><dt>线性迭代峰值</dt><dd>{observed.maximum_linear_iterations}</dd></>}
      {(observed.maximum_nonlinear_iterations ?? observed.maximum_iterations) !== undefined &&
        <><dt>非线性迭代峰值</dt><dd>{observed.maximum_nonlinear_iterations ?? observed.maximum_iterations}</dd></>}
      {observed.steps !== undefined && <><dt>时间步</dt><dd>{observed.steps} · 末时刻 {observed.last_time} s</dd></>}
      {observed.adaptive_trials !== undefined && observed.adaptive_trials > 0 && <><dt>自适应试步</dt><dd>{observed.adaptive_trials}</dd></>}
      {observed.time_steps !== undefined && <><dt>物理时间步</dt><dd>{observed.time_steps}
        {observed.time_step_s !== undefined && ` × ${observed.time_step_s} s`}</dd></>}
      {observed.physical_cfl !== undefined && <><dt>物理时间 CFL</dt><dd>{observed.physical_cfl}</dd></>}
      {observed.minimum_time_step !== undefined && observed.maximum_time_step !== undefined && <><dt>实际增量范围</dt>
        <dd>{quantity.format(observed.minimum_time_step)}–{quantity.format(observed.maximum_time_step)}</dd></>}
      {observed.minimum_cfl !== undefined && <><dt>CFL 范围</dt><dd>{observed.minimum_cfl}–{observed.maximum_cfl}</dd></>}
      {observed.final_log_density_residual !== undefined && <><dt>密度对数残差</dt>
        <dd>{quantity.format(observed.final_log_density_residual)}{observed.density_target_reached !== undefined
          && ` · ${observed.density_target_reached ? '达到目标' : '未达到目标'}`}</dd></>}
    </dl>
  </div>;
}

export default function JobSolverResults({ caseId, jobId, attemptId, publicationId, solver }: {
  caseId: number; jobId: number; attemptId: string; publicationId: string; solver: NativeSolver;
}) {
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [failure, setFailure] = useState('');
  const [fileFailure, setFileFailure] = useState('');
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState('');
  const [preview, setPreview] = useState<{ path: string; text: string; partial: boolean } | null>(null);
  const transfer = useRef<AbortController | null>(null);
  const previewBody = useRef<HTMLPreElement>(null);
  const previewTrigger = useRef<HTMLButtonElement | null>(null);
  const path = `/cases/${caseId}/provenance/artifacts/${publicationId}`;
  const sealedSif = trace?.electrodes?.native_input_sha256 ?? trace?.coupling?.native_input_sha256
    ?? trace?.electrostatic?.native_input_sha256 ?? (solver === 'elmer' ? trace?.numerical?.native_input_sha256 : undefined);

  useEffect(() => { if (preview) previewBody.current?.focus(); }, [preview]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setFailure('');
    void jobRecoveryRequest<Trace>(path, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      const publication = result.publication;
      if (publication.id !== publicationId || publication.job_id !== jobId || publication.attempt_id !== attemptId
        || !publication.kind.startsWith(`${solver}:`)) throw new Error('发布记录与所选作业不一致。');
      setTrace(result);
    }).catch(error => {
      if (!controller.signal.aborted) setFailure(readError(error));
    });
    return () => { controller.abort(); transfer.current?.abort(); };
  }, [open, reload, path, publicationId, jobId, attemptId, solver]);

  async function readFile(file: string, artifact: Artifact, kind: 'preview' | 'download') {
    if (transfer.current) return;
    const controller = new AbortController();
    transfer.current = controller;
    setBusy(file); setFileFailure('');
    if (kind === 'preview') setPreview(null);
    try {
      const token = localStorage.getItem('vf_token');
      const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
      const partial = kind === 'preview' && artifact.size > previewLimit;
      if (partial) headers.Range = `bytes=0-${previewLimit - 1}`;
      const response = await fetch(`/api/v1${path}/file?${new URLSearchParams({ path: file })}`, {
        headers, credentials: 'same-origin', signal: controller.signal,
      });
      if (!response.ok) throw new ApiError(response.status, await response.json().then(body => body.detail).catch(() => null));
      if (response.status !== (partial ? 206 : 200) || !response.headers.get('X-Vibeflow-File-Version')) {
        await response.body?.cancel();
        throw new Error('文件回执无法核验。');
      }
      const bytes = await response.arrayBuffer();
      if (controller.signal.aborted) return;
      if (bytes.byteLength !== (partial ? previewLimit : artifact.size)) throw new Error('文件大小与发布清单不一致。');
      if (kind === 'preview') setPreview({ path: file, text: new TextDecoder().decode(bytes), partial });
      else saveDerivative(bytes, file.split('/').at(-1) || 'solver-result');
    } catch (error) {
      if (!controller.signal.aborted) setFileFailure(readError(error));
    } finally {
      transfer.current = null;
      if (!controller.signal.aborted) setBusy('');
    }
  }

  return <details className="job-solver-results" onToggle={event => {
    const opened = event.currentTarget.open;
    setOpen(opened);
    if (!opened) { transfer.current?.abort(); setTrace(null); setPreview(null); setFileFailure(''); setBusy(''); }
  }}>
    <summary>求解成果 · {nativeSolvers[solver].label}</summary>
    {open && <section aria-label="求解成果" aria-busy={!!busy || (!trace && !failure)}>
      {failure ? <><p role="alert">{failure}</p><button type="button" onClick={() => setReload(value => value + 1)}>重试读取成果</button></>
        : !trace ? <p role="status">正在读取发布清单…</p> : <>
          <p>第 {trace.publication.generation} 版 · {trace.publication.active ? '当前发布' : '历史发布'}</p>
          <label>发布 ID<input readOnly value={publicationId} onFocus={event => event.currentTarget.select()} /></label>
          {trace.contact && <div role="region" aria-label="机械接触" className="job-solver-setup">
            <strong>机械接触</strong>
            {trace.contact.interfaces.map(contact => <dl key={contact.name}>
              <dt>接口</dt><dd>{contact.name}</dd>
              <dt>从面 → 主面</dt><dd>{contact.dependent_surface} → {contact.independent_surface}</dd>
              <dt>法向刚度</dt><dd>{quantity.format(contact.normal_stiffness_pa_per_m)} Pa/m</dd>
            </dl>)}
          </div>}
          {trace.pressure_transfer && <div role="region" aria-label="OpenFOAM 压力载荷" className="job-solver-setup">
            <strong>OpenFOAM 压力载荷</strong>
            <dl>
              <dt>源边界</dt><dd>{trace.pressure_transfer.patch} · {quantity.format(trace.pressure_transfer.time)} s</dd>
              {trace.pressure_transfer.source_field === 'pa' && <><dt>源字段</dt><dd>声压 pa</dd></>}
              <dt>目标节点集</dt><dd>{trace.pressure_transfer.node_set}</dd>
              {trace.pressure_transfer.nodal_loads && <><dt>载荷分配</dt><dd>对应 {trace.pressure_transfer.nodal_loads.positions_m.length} 个节点</dd></>}
              <dt>合力 (X, Y, Z)</dt><dd>{trace.pressure_transfer.force_n.map(value => quantity.format(value)).join(', ')} N</dd>
            </dl>
            <label>上游发布 ID<input readOnly value={trace.pressure_transfer.publication_id}
              onFocus={event => event.currentTarget.select()} /></label>
          </div>}
          {trace.electrodes && <div role="region" aria-label="电极边界" className="job-solver-setup">
            <strong>电极边界</strong>
            {trace.electrodes.values.map(electrode => <dl key={electrode.tag}>
              <dt>电极</dt><dd>{electrode.name} · 网格标签 {electrode.tag}</dd>
              <dt>面积</dt><dd>{quantity.format(electrode.area_m2)} m²</dd>
              <dt>{electrodeQuantity[electrode.kind].label}</dt>
              <dd>{quantity.format(electrode.value_si)} {electrodeQuantity[electrode.kind].unit}</dd>
              {electrode.kind === 'current' && <>
                <dt>电流密度</dt><dd>{quantity.format(electrode.native_value)} A/m²</dd>
              </>}
              {electrode.kind === 'impedance' && <>
                <dt>外部电势</dt><dd>{quantity.format(electrode.external_potential_v)} V</dd>
                <dt>单位面积电导</dt><dd>{quantity.format(electrode.native_value)} S/m²</dd>
              </>}
            </dl>)}
          </div>}
          {trace.electrostatic && <div role="region" aria-label="静电介质" className="job-solver-setup">
            <strong>静电介质</strong>
            <dl>
              <dt>材料</dt><dd>{trace.electrostatic.material} · 原生材料 {trace.electrostatic.native_material}</dd>
              <dt>相对介电常数</dt><dd>{quantity.format(trace.electrostatic.relative_permittivity)}</dd>
              <dt>真空介电常数</dt><dd>{scientific.format(trace.electrostatic.vacuum_permittivity_f_m)} F/m</dd>
            </dl>
          </div>}
          {trace.coupling && <div role="region" aria-label="焦耳热耦合" className="job-solver-setup">
            <strong>焦耳热耦合</strong>
            <dl>
              <dt>求解顺序</dt><dd>电流 {trace.coupling.current_solver} → 热传导 {trace.coupling.heat_solver}</dd>
              {trace.coupling.body !== undefined && <><dt>原生体编号</dt><dd>{trace.coupling.body}</dd></>}
              {trace.coupling.bodies?.map(body => <Fragment key={body.body}>
                <dt>体 {body.body} 电导率</dt><dd>{quantity.format(body.conductivity_s_m)} S/m</dd>
              </Fragment>)}
            </dl>
            {trace.coupling.electrical_interfaces?.map(item => <dl key={item.name}>
              <dt>电连续界面</dt><dd>{item.regions.join(' ↔ ')}</dd>
              <dt>原生边界 {item.boundary_tag}</dt><dd>{item.shared_faces} 个共享面</dd>
            </dl>)}
          </div>}
          {trace.numerical && <NumericalEvidence run={trace.numerical} />}
          {trace.element_families && <div role="region" aria-label="结构单元" className="job-solver-setup">
            <strong>结构单元</strong>
            <dl>
              <dt>声明的单元族</dt><dd>{trace.element_families.declared.join('、')}</dd>
              <dt>CalculiX 类型</dt><dd>{trace.element_families.native_types.join('、')}</dd>
            </dl>
          </div>}
          {trace.mesh_adaptivity && <div role="region" aria-label="网格自适应" className="job-solver-setup">
            <strong>网格自适应</strong>
            <dl>
              <dt>方法</dt><dd>{trace.mesh_adaptivity.mode === 'goalOriented'
                ? `目标驱动 · ${trace.mesh_adaptivity.native_mode}`
                : `${trace.mesh_adaptivity.mode} · ${trace.mesh_adaptivity.cycles} 轮`}</dd>
              <dt>活动单元</dt><dd>{trace.mesh_adaptivity.observed.cells[0]} → {trace.mesh_adaptivity.observed.cells.at(-1)}</dd>
              <dt>自由度</dt><dd>{trace.mesh_adaptivity.observed.dofs[0]} → {trace.mesh_adaptivity.observed.dofs.at(-1)}</dd>
              {trace.mesh_adaptivity.goal && trace.mesh_adaptivity.observed.goal && <>
                <dt>探针目标</dt><dd>{trace.mesh_adaptivity.goal.postprocessor} · {quantity.format(trace.mesh_adaptivity.goal.target)}</dd>
                <dt>达到目标</dt><dd>{trace.mesh_adaptivity.observed.goal.first_met_cycle === 0 ? '初始网格'
                  : `第 ${trace.mesh_adaptivity.observed.goal.first_met_cycle} 轮`}</dd>
                <dt>末态绝对误差</dt><dd>{scientific.format(trace.mesh_adaptivity.observed.goal.final_absolute_error)}</dd>
              </>}
            </dl>
          </div>}
          {sealedSif && <label>封存 SIF SHA-256<input readOnly value={sealedSif} onFocus={event => event.currentTarget.select()} /></label>}
          <ul>{Object.entries(trace.files).sort(([a], [b]) => a.localeCompare(b)).map(([file, artifact]) => <li key={file}>
            <span className="job-solver-path">{file}</span>
            <div className="job-solver-actions">
              {canPreview(file, solver) && <button type="button" disabled={!!busy} aria-label={`预览 ${file}`}
                onClick={event => { previewTrigger.current = event.currentTarget; void readFile(file, artifact, 'preview'); }}>预览</button>}
              <button type="button" disabled={!!busy} aria-label={`下载原件 ${file}`}
                onClick={() => void readFile(file, artifact, 'download')}>下载原件</button>
            </div>
          </li>)}</ul>
        </>}
      {busy && <p role="status">正在核验并读取 {busy}…</p>}
      {fileFailure && <p role="alert">{fileFailure}</p>}
      {preview && <div className="job-solver-preview">
        <p>{preview.path}{preview.partial && ' · 前 128 KiB'}</p>
        <pre ref={previewBody} tabIndex={0} aria-label={`${preview.path} 的内容`}>{preview.text}</pre>
        <button type="button" onClick={() => { setPreview(null); previewTrigger.current?.focus(); }}>关闭预览</button>
      </div>}
    </section>}
  </details>;
}
