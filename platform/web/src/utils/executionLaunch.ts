export type LaunchMode = 'single' | 'batch' | 'workflow' | 'native';
export type LaunchRuntime = 'windows' | 'wsl' | 'wsl-sandbox';
export type LaunchTarget = 'geometry' | 'mesh' | 'solve' | 'postprocess' | 'report';
export const postprocessScriptCommand = 'MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1 python3 -u -B D-post/postprocess.py';
export const nativeSolvers = {
  calculix: { label: 'CalculiX', suffix: '.inp', restart: null },
  su2: { label: 'SU2', suffix: '.cfg', restart: null },
  elmer: { label: 'Elmer', suffix: '.sif', restart: null },
  moose: { label: 'MOOSE', suffix: '.i', restart: '_checkpoint.zip' },
  'code-aster': { label: 'Code_Aster', suffix: '.export', restart: '_base.zip' },
} as const;
export type NativeSolver = keyof typeof nativeSolvers;
export interface NativeRestart { publication_id: string; checkpoint_path: string }
export interface PressureTransfer { publication_id: string; surface: string; patch: string; time: number; node_set: string;
  distribution?: 'uniformResultant' | 'conformalNodal' }
interface NativeBody { solver: NativeSolver; input_path: string; restart?: NativeRestart; pressure_transfer?: PressureTransfer }
interface Budget { timeout_seconds: number; max_log_bytes: number }
export type SingleJobType = 'shell' | 'solve' | 'convert';
interface SingleBody extends Budget { type: SingleJobType; command: string; runtime: LaunchRuntime; run_dir?: string }
export type PreparedBatchOperation = Budget & { run_dir: string } & (
  { kind: 'serial'; solver: string } | { kind: 'mpi'; solver: string; subdomains: number } | { kind: 'udf'; name: string });
type BatchBody = { max_concurrent: number } & (
  { commands: string[]; runtime: LaunchRuntime; operations?: never } |
  { operations: PreparedBatchOperation[]; commands?: never; runtime?: never });
interface WorkflowBody { target: LaunchTarget; expected_revision: number; run_dir: string; budget: Budget;
  parameters: { geometry_mode: 'preview' | 'export'; solve_mode?: 'serial' | 'mpi' | 'udf'; subdomains?: number; udf_name?: string; report_mode?: 'compose' | 'markdown'; placement?: 'remote' } }
export type LaunchRequest = { schema: 1; userId: number; caseId: number; key: string } & (
  { mode: 'single'; body: SingleBody } | { mode: 'batch'; body: BatchBody } | { mode: 'workflow'; body: WorkflowBody } |
  { mode: 'native'; body: NativeBody });
export interface LaunchReceipt { kind: LaunchMode; case_id: number; id: number | string }
export interface LaunchLookup extends LaunchReceipt { found: boolean }
export const launchStorageKey = (userId: number, caseId: number) => `vf-execution-launch-v1:${userId}:${caseId}`;
export const launchLabel = (mode: LaunchMode) => ({ single: '单作业', batch: '批次', workflow: '工作流', native: '开源求解' })[mode];
export const launchPath = (request: LaunchRequest) => `/cases/${request.caseId}/${request.mode === 'native'
  ? `${request.body.solver}:solve` : ({ single: 'jobs', batch: 'jobs:batch', workflow: 'workflow-runs' })[request.mode]}`;
export function launchPayload(request: LaunchRequest) {
  if (request.mode !== 'native') return request.body;
  const { solver, input_path, restart, pressure_transfer } = request.body;
  if (solver === 'su2') return { config_path: input_path };
  return { input_path, ...(restart ? { restart } : {}), ...(pressure_transfer ? { pressure_transfer } : {}) };
}
export const isNativeSolver = (value: unknown): value is NativeSolver => typeof value === 'string'
  && Object.prototype.hasOwnProperty.call(nativeSolvers, value);
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const uuid = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(v);
const integer = (v: unknown, min: number, max: number) => Number.isSafeInteger(v) && Number(v) >= min && Number(v) <= max;
const runtime = (v: unknown) => ['windows', 'wsl', 'wsl-sandbox'].includes(String(v));
const command = (v: unknown) => typeof v === 'string' && v.trim().length > 0 && v.length <= 4000;
const directory = (v: unknown) => typeof v === 'string' && v.length > 0 && v.length <= 1024
  && !v.startsWith('/') && !/[\\:\u0000-\u001f]/.test(v) && v.split('/').every(part => part && part !== '.' && part !== '..');
const budget = (v: unknown) => record(v) && typeof v.timeout_seconds === 'number' && Number.isFinite(v.timeout_seconds)
  && v.timeout_seconds > 0 && v.timeout_seconds <= 86400 && integer(v.max_log_bytes, 1024, 64 * 1024 * 1024);

function validPreparedOperations(operations: unknown[]) {
  const paths: string[] = [];
  return operations.every(operation => {
    if (!record(operation) || !budget(operation) || !integer(operation.timeout_seconds, 1, 86400)
      || typeof operation.run_dir !== 'string' || !directory(operation.run_dir)) return false;
    const path = operation.run_dir.toLowerCase();
    if (path.split('/')[0] === '.vibeflow' || paths.some(used => used === path || used.startsWith(`${path}/`) || path.startsWith(`${used}/`))) return false;
    paths.push(path);
    if (operation.kind === 'udf') return typeof operation.name === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(operation.name);
    return typeof operation.solver === 'string' && /^[A-Za-z][A-Za-z0-9_+-]*$/.test(operation.solver)
      && (operation.kind === 'serial' || (operation.kind === 'mpi' && integer(operation.subdomains, 1, 64)));
  });
}

export function validateLaunch(value: unknown, userId: number, caseId: number): asserts value is LaunchRequest {
  if (!record(value) || value.schema !== 1 || value.userId !== userId || value.caseId !== caseId
    || typeof value.key !== 'string' || !/^[0-9a-f-]{36}$/.test(value.key) || !record(value.body)) throw new Error('本机提交记录无法核验，请保留记录并核对任务历史。');
  const body = value.body;
  let valid = false;
  if (value.mode === 'single') valid = ['shell', 'solve', 'convert'].includes(String(body.type)) && command(body.command)
    && runtime(body.runtime) && budget(body) && (body.type === 'solve' ? directory(body.run_dir) : body.run_dir === undefined)
    && (body.type !== 'convert' || (body.command === postprocessScriptCommand && body.runtime === 'wsl-sandbox'));
  if (value.mode === 'batch') {
    const items = body.operations ?? body.commands;
    valid = integer(body.max_concurrent, 1, 4) && Array.isArray(items) && items.length >= 1 && items.length <= 50
      && (body.operations === undefined ? runtime(body.runtime) && items.every(command)
        : body.commands === undefined && body.runtime === undefined && validPreparedOperations(items));
    if (!valid && body.operations !== undefined) throw new Error('请检查每项的求解目录、方式和资源限制。批次内的目录须各自独立。');
  }
  if (value.mode === 'workflow' && record(body.parameters)) {
    const p = body.parameters;
    valid = ['geometry', 'mesh', 'solve', 'postprocess', 'report'].includes(String(body.target))
      && integer(body.expected_revision, 0, Number.MAX_SAFE_INTEGER) && directory(body.run_dir) && budget(body.budget)
      && ['preview', 'export'].includes(String(p.geometry_mode))
      && (p.solve_mode === undefined || ['serial', 'mpi', 'udf'].includes(String(p.solve_mode)))
      && (p.solve_mode === 'mpi' ? integer(p.subdomains, 1, 64) : p.subdomains === undefined)
      && (p.solve_mode === 'udf' ? typeof p.udf_name === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(p.udf_name) : p.udf_name === undefined)
      && (p.placement === undefined || (p.placement === 'remote' && ['solve', 'postprocess', 'report'].includes(String(body.target))))
      && (p.report_mode === undefined || ['compose', 'markdown'].includes(String(p.report_mode)));
  }
  if (value.mode === 'native') {
    if (!isNativeSolver(body.solver) || typeof body.input_path !== 'string' || !directory(body.input_path)
      || !body.input_path.toLowerCase().endsWith(nativeSolvers[body.solver].suffix))
      throw new Error('请选择该求解器支持的算例输入文件。');
    const suffix = nativeSolvers[body.solver].restart;
    const restart = body.restart;
    if (restart !== undefined && (!suffix || !record(restart) || !uuid(restart.publication_id)
      || typeof restart.checkpoint_path !== 'string' || !directory(restart.checkpoint_path) || !restart.checkpoint_path.endsWith(suffix)))
      throw new Error('请填写有效的发布 ID 和续算文件路径。');
    const transfer = body.pressure_transfer;
    if (transfer !== undefined && (body.solver !== 'calculix' || !record(transfer) || !uuid(transfer.publication_id)
      || typeof transfer.surface !== 'string' || !transfer.surface || transfer.surface.length > 128
      || typeof transfer.patch !== 'string' || !transfer.patch || transfer.patch.length > 128
      || typeof transfer.time !== 'number' || !Number.isFinite(transfer.time) || transfer.time < 0
      || (transfer.distribution !== undefined && transfer.distribution !== 'uniformResultant' && transfer.distribution !== 'conformalNodal')
      || typeof transfer.node_set !== 'string' || !/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(transfer.node_set)))
      throw new Error('请选择已发布的压力曲面积分，并填写有效的 CalculiX 节点集。');
    valid = true;
  }
  if (!valid) throw new Error('提交参数不完整：请检查命令长度、目录、资源限制及工作流参数。');
}
export function readLaunch(userId: number, caseId: number): LaunchRequest | null {
  const raw = sessionStorage.getItem(launchStorageKey(userId, caseId));
  if (raw === null) return null;
  if (raw.length > 240000) throw new Error('本机提交记录过大，请保留记录并核对任务历史。');
  const value: unknown = JSON.parse(raw); validateLaunch(value, userId, caseId); return value;
}
export function saveLaunch(request: LaunchRequest) {
  validateLaunch(request, request.userId, request.caseId);
  const raw = JSON.stringify(request);
  if (raw.length > 240000) throw new Error('提交内容过大，请缩小批次。');
  const key = launchStorageKey(request.userId, request.caseId);
  sessionStorage.setItem(key, raw);
  if (sessionStorage.getItem(key) !== raw) throw new Error('本机提交记录未能保存，尚未发送请求。');
}
export function verifyLaunchReceipt(request: LaunchRequest, value: unknown): LaunchReceipt {
  const invalid = '提交回执归属无法核验，请读取原提交结果。';
  if (!record(value) || value.case_id !== request.caseId) throw new Error(invalid);
  const id = request.mode === 'native' && value.backend === request.body.solver ? value.job_id : value.id;
  if (request.mode === 'native' && value.backend !== request.body.solver && value.kind !== 'native') throw new Error(invalid);
  if (request.mode === 'single' || request.mode === 'native') {
    if (!integer(id, 1, Number.MAX_SAFE_INTEGER)) throw new Error(invalid);
  } else if (typeof id !== 'string' || !(request.mode === 'batch' ? /^[0-9a-f]{32}$/ : /^[0-9a-f-]{36}$/).test(id)) {
    throw new Error(invalid);
  }
  return { kind: request.mode, case_id: request.caseId, id: id as number | string };
}
