export interface WorkflowRunSummary {
  id: string; case_id: number; target: string; status: string; version: number;
  run_dir: string; expected_revision: number; error_code: string; detail: string;
  created_at: string; ended_at: string | null;
}
export interface WorkflowAttempt {
  id: string; number: number; job_id: number; phase: string; reason_code: string;
  exit_code: number | null; job_status: string; completion_error: string | null;
}
export interface WorkflowStep {
  id: string; key: string; status: string; version: number; error_code: string; detail: string;
  active_attempt_id: string | null; confirmation_state: string | null;
  confirmation_revision: number | null;
  confirmation: { user_id: number; state: string; from_revision: number; to_revision: number } | null;
  publication: { id:string; kind:string; generation:number; status:string } | null;
  execution_mode: string; reused_publication: { id: string; generation: number } | null;
  attempts: WorkflowAttempt[];
}
export interface WorkflowRunDetail extends WorkflowRunSummary {
  steps: WorkflowStep[];
  resumption: { source_run_id: string; mode: string; checkpoint_time?: string } | null;
  recomputation: { source_run_id: string; reused: Record<string, { id: string; generation: number }>; rebuild: string[] } | null;
  legacy_migration: { source_run_id: string } | null;
}
export const workflowTerminal = (status: string) => ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(status);
export const workflowReasonLabel = (code: string) => ({
  process_exit_failed: '执行进程返回失败，请查看作业日志中的原始错误。',
  dependency_failed: '前序步骤未成功，后续步骤已停止。',
  cancel_requested: '已请求取消此步骤。',
  cancelled: '此步骤已取消。',
  input_stale: '输入已变化，请在恢复入口核对输入版本与重新计算范围。',
  evidence_invalid: '执行证据未通过核验，请保留原记录并查看作业恢复检查。',
  resource_limit: '执行资源预算已耗尽，请核对日志与恢复方案。',
  timeout: '执行已超过原定期限，请核对日志与恢复方案。',
})[code] ?? '结果原因需要进一步核查，请查看原始诊断与作业日志。';
export function confirmationStepText(step: WorkflowStep): string {
  if (step.status === 'succeeded') return '已完成阶段确认。此步骤无需执行作业。';
  if (step.status === 'awaiting_confirmation') return `等待明确确认${step.confirmation_state}阶段，此步骤不会自动推进。`;
  if (step.status === 'cancelled') return '此步骤已取消，此阶段尚未确认。';
  return '此阶段尚未确认，无执行作业；需先完成前序步骤。';
}
export const workflowStepLabel = (step: Pick<WorkflowStep, 'key' | 'confirmation_state'>) =>
  step.confirmation_state ? `确认${step.confirmation_state}阶段` :
    ({ preflight: '输入检查', geometry: '几何', mesh: '网格', 'udf-build': '编译UDF', solve: '求解', postprocess: '后处理', report: '报告' })[step.key] ?? step.key;
export function workflowNextStep(run: WorkflowRunDetail): string {
  if (run.status === 'cancelling') return '已请求取消，正在核验进程停止并归档输出。完成前请等待，不要重新启动。';
  if (run.steps.some(step => step.status === 'finalizing' || step.attempts.some(attempt => attempt.id === step.active_attempt_id && attempt.phase === 'finalizing')))
    return '计算输出正在归档并核验发布；收尾完成后才会显示成功。';
  if (run.status === 'awaiting_confirmation') return '请在算例阶段入口核对产物并明确确认；流程不会自动确认后续阶段。';
  if (run.status === 'queued') return '流程已登记，等待派发。请等待状态更新，避免重复启动。';
  if (run.status === 'interrupted') return '流程已中断。请先查看当前尝试的恢复检查，核实原进程与输出后再决定下一步。';
  if (run.status === 'failed') return '流程失败，原输入与作业记录已保留。请查看失败步骤及其作业日志。';
  if (run.status === 'cancelled') return '流程已取消，原输入、尝试与日志保留在历史记录中。';
  if (run.status === 'succeeded') return '流程已完成，可在各步骤的作业中查看输出与发布核验记录。';
  return '流程正在执行，步骤状态会自动刷新。';
}
