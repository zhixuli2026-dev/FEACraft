export interface RecoveryRun {
  id: string; case_id: number; status: string; version: number; target: string; run_dir: string;
  expected_revision: number; error_code: string; detail: string;
  parameters: Record<string, unknown>;
  recomputation?: { source_run_id: string; source_run_version: number; reused: Record<string, unknown>; rebuild: string[] };
  legacy_migration?: { source_run_id: string; source_run_version: number };
  recomputation_reset?: { confirmed_by: number; from_revision: number; to_state: string };
  legacy_migration_reset?: { confirmed_by: number; from_revision: number; to_state: string };
  steps: { id: string; key: string; status: string; detail: string; confirmation_state: string | null;
    execution_mode: string; attempts: { id: string; job_id: number; phase: string }[] }[];
}
export interface WorkflowRecoveryContext {
  run: RecoveryRun; current_revision: number; current_stage: string;
  budget: { timeout_seconds: number; max_log_bytes: number } | null;
  action: 'recompute' | 'migrate' | null; reason: { code: string; message: string } | null;
  migration: { required: boolean; current_event_sequence?: number; source_plan_sha256?: string } | null;
  source: Pick<RecoveryRun, 'id' | 'status'> | null;
  successors: { items: (Pick<RecoveryRun, 'id' | 'status' | 'target'> & { audit_event_id: string })[]; next_before_seq: number | null };
}
export interface WorkflowRecoveryBody {
  expected_version: number; expected_revision: number; confirmed_reset: true;
  target: string; run_dir: string; parameters: Record<string, unknown>;
  budget: { timeout_seconds: number; max_log_bytes: number };
  expected_event_sequence?: number; expected_source_plan_sha256?: string;
}
export interface PendingWorkflowRecovery {
  schema: 1; runId: string; caseId: number; action: 'recompute' | 'migrate'; key: string; body: WorkflowRecoveryBody;
}
export const workflowRecoveryKey = (user: number, caseId: number, runId: string) => `vf-workflow-recovery-v1:${user}:${caseId}:${runId}`;
export function readPendingWorkflowRecovery(key: string, runId: string, caseId: number): PendingWorkflowRecovery | null {
  const raw = sessionStorage.getItem(key);
  if (!raw) return null;
  const value = JSON.parse(raw) as PendingWorkflowRecovery, body = value?.body;
  if (raw.length > 12000 || value.schema !== 1 || value.runId !== runId || value.caseId !== caseId
    || !['recompute', 'migrate'].includes(value.action) || !/^[0-9a-f-]{36}$/.test(value.key)
    || !body || body.confirmed_reset !== true || !Number.isSafeInteger(body.expected_version) || body.expected_version < 0
    || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 0
    || !['geometry', 'mesh', 'solve', 'postprocess', 'report'].includes(body.target)
    || typeof body.run_dir !== 'string' || !body.run_dir || !body.parameters || typeof body.parameters !== 'object' || Array.isArray(body.parameters)
    || !body.budget || !Number.isFinite(body.budget.timeout_seconds) || body.budget.timeout_seconds <= 0 || body.budget.timeout_seconds > 86400
    || !Number.isSafeInteger(body.budget.max_log_bytes) || body.budget.max_log_bytes < 1024 || body.budget.max_log_bytes > 64 * 1024 * 1024
    || (value.action === 'migrate' && (!Number.isSafeInteger(body.expected_event_sequence) || body.expected_event_sequence! < 1
      || !/^[0-9a-f]{64}$/.test(body.expected_source_plan_sha256 ?? '')))) throw new Error('工作流确认记录无法核验，请保留本机记录。');
  return value;
}
export const workflowTargetLabel = (value: string) => ({ preflight: '输入检查', geometry: '几何', mesh: '网格', solve: '求解', postprocess: '后处理', report: '报告' })[value] ?? value;
export const workflowStatusLabel = (value: string) => ({ queued: '排队', running: '运行中', awaiting_confirmation: '等待阶段确认',
  claimed: '已领取，等待启动', starting: '启动中',
  cancelling: '正在取消', succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '已中断', ready: '就绪', pending: '等待', blocked: '受前序步骤阻挡', finalizing: '收尾中' })[value] ?? value;
