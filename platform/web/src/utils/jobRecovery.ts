import { ApiError } from "../api";
import type { InputRecoveryReceipt } from './inputRecovery';

export interface RecoveryJob {
  id: number; status: string; phase?: string | null; completion_pending?: boolean;
  record_issues?: { field: string; code: string; message: string }[];
  run_id?: string | null;
  recovery?: { state: string; reason: string; next_step: string } | null;
  input_recoveries?: InputRecoveryReceipt[];
  attempt?: {
    id: string; number: number; root_job_id: number; phase: string; reason_code: string;
    retry_of_attempt_id: string | null;
    workspace?: unknown;
    operation?: { action: string; source_job_id: number; source_attempt_id: string; checkpoint_time?: string };
    outputs?: {
      files?: Record<string, { size: number; sha256: string }>;
      finalizing_stage?: unknown; output_preparation?: unknown;
      bundle_preparation?: unknown; finalization_intent?: unknown;
      completion?: { state?: string; alias_error?: string | null };
    };
  } | null;
}
export interface PreparationPlan {
  state: "preparation_pending" | "captured"; attempt_id: string; stage_sha256: string;
  owner_observation: { state: string; reason: string }; inventory_sha256?: string;
  finalization_pending: boolean;
}
export interface OutputPlan {
  state: string; attempt_id: string; inventory_sha256: string; finalization_pending: boolean;
  owner_observation?: { state: string; reason: string }; archive_pending?: boolean;
}
export interface DispositionReceipt {
  state: string; event_id: string; recorded_at: string; content_drained: false;
  execution_authorized: false;
}
export interface DispositionPlan {
  attempt_id: string | null; snapshot_sha256: string; issues: string[];
  available_actions: string[]; disposition: DispositionReceipt | null;
}
export interface PublicationPlan {
  state: string; database_status?: string; active?: boolean; next_step?: string;
  automatic_replay: false;
}
export interface OperationOption {
  available: boolean; parameters_required?: boolean;
  reason: { code: string; message: string } | null;
}
export interface JobOperations {
  job_id: number; source_attempt_id: string | null; source_version: number | null;
  expected_revision: number; expected_event_sequence: number;
  retry: OperationOption; rerun: OperationOption;
  resume: OperationOption & { supports_latest?: boolean };
  reprepare: OperationOption & { handler_choices: { key: string; version: string }[] };
}
export type RepeatAction = "retry" | "rerun" | "resume";
export interface RepeatBody {
  action: RepeatAction; source_attempt_id: string; source_version: number;
  expected_revision?: number; expected_event_sequence?: number;
  parameters?: Record<string, unknown>;
  reprepare_handler?: { key: string; version: string };
}
export interface PendingRepeat { schema: 1; jobId: number; key: string; body: RepeatBody }

export async function jobRecoveryRequest<T>(path: string, options: {
  signal?: AbortSignal; body?: unknown; key?: string;
} = {}): Promise<T> {
  const token = localStorage.getItem("vf_token");
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.key) headers["Idempotency-Key"] = options.key;
  const response = await fetch("/api/v1" + path, {
    method: options.body === undefined ? "GET" : "POST", headers, signal: options.signal,
    credentials: "same-origin", body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (!response.ok) {
    let detail: unknown = "请求未完成";
    try { const value = await response.json(); detail = value.detail ?? detail; } catch { /* Plain failure has no public details. */ }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

const REASONS: Record<string, string> = {
  owner_live: "原执行者仍在工作，请等待它完成收尾。",
  legacy_unverified: "旧记录缺少可信执行信息，需要人工核查。",
  lineage_unverified: "任务与尝试的关联无法核验，请保留现场。",
  input_recovery_required: "原输入封存不完整，需要核对输入后重新准备请求。",
  owner_unverified: "无法确认原执行者已经退出，暂时不能接管。",
  process_cleanup_unverified: "任务已结束，但尚未取得完整停止证明。",
  publication_recovery_required: "产物发布尚未完成，原产物已保留。",
  registration_recovery_required: "启动登记尚未完整，请先核查原执行状态。",
  state_recovery_required: "任务状态需要人工核验，现有记录已保留。",
  finalizing_io_unverified: "仍有收尾读写的结束状态待核查。",
  finalizing_stage_recovery_required: "原程序已停止，证据和日志尚待接续。",
  early_finalization_unavailable: "原文件或停止证明无法核验，请保留现场并核查原副本。",
  early_finalization_manual_review: "旧回调或暂存同步无法安全接续，需要人工核查。",
  early_finalization_unverified: "原任务没有完整的收尾阶段记录，需要人工核查。",
  finalizing_file_conflict: "原文件与收尾计划不同，已有内容已保留。",
  early_finalization_busy: "另一个恢复请求正在操作，请稍后刷新。",
  output_recovery_busy: "另一个输出恢复请求正在进行，请稍后刷新。",
  output_archive_busy: "原归档仍在写入，请等待写入真正结束。",
  output_cache_unavailable: "原执行缓存缺失或已变化，不能通过重新计算冒充恢复。",
  output_recovery_unavailable: "原输出或归档证明无法核验，请保留现有文件。",
  finalization_recovery_unavailable: "原日志或校验停止状态无法核验，请保留现场。",
  recovery_required: "原进程、读写或发布尚未核验结束，请刷新方案并核对保留记录。",
  state_conflict: "任务已变化，请刷新方案后重新确认。",
  input_stale: "输入版本已变化，请刷新并重新确认当前输入。",
  revision_conflict: "算例版本已变化，请刷新方案后重新确认。",
  replacement_exists: "原请求已经关联独立新作业，请查看关联记录。",
  workflow_recovery_required: "此请求属于工作流，请在原流程中核对输入并重新计算。",
  invalid_request: "处理器参数未通过校验。请取消被拒绝的确认，按所选处理器要求补全参数后重新确认。",
  cancelled_archive_pending: "取消已受理，待归档数据和容量仍保留，需要核验原上传的结束状态。",
  cancelled_output_archive_pending: "原任务已取消，仍有输出复制需要核查。",
  resource_limit: "原资源预算已耗尽，恢复不会增加原期限或额度。",
  checkpoint_required: "此任务尚无可核验的续算检查点。",
  checkpoint_disabled: "原尝试的算例设置已禁用检查点续算。",
};
export const jobRecoveryReason = (reason: string) => REASONS[reason] ?? "原任务需要进一步核查，请查看保留记录和可用操作。";
export function jobRecoveryError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后核对任务记录。";
    if (error.status === 403 || error.status === 404) return "当前账户无法操作该任务，请核对算例权限。";
    if (error.code && REASONS[error.code]) return REASONS[error.code];
    if (error.status === 422 || error.status === 400) return "操作参数未通过校验，请检查参数后重新确认。";
    return "操作结果暂时无法确认，请刷新方案核对记录后再继续。";
  }
  return "网络连接中断，操作可能已提交。请核对任务记录，勿创建新的确认标识。";
}
export const repeatLabel = (action: RepeatAction) => ({ retry: "重试原输入", rerun: "用当前输入重跑", resume: "从检查点续算" })[action];
export const repeatBodyLabel = (body: RepeatBody) => body.reprepare_handler ? '用当前输入重新准备处理器' : repeatLabel(body.action);
export const pendingRepeatKey = (userId: number, caseId: number, jobId: number) => `vf-job-repeat-v1:${userId}:${caseId}:${jobId}`;
export function readPendingRepeat(storageKey: string, jobId: number): PendingRepeat | null {
  const raw = sessionStorage.getItem(storageKey);
  if (!raw) return null;
  const value = JSON.parse(raw) as PendingRepeat;
  const body = value?.body;
  if (value.schema !== 1 || value.jobId !== jobId || !/^[0-9a-f-]{36}$/.test(value.key)
    || !body || !["retry", "rerun", "resume"].includes(body.action)
    || !/^[0-9a-f-]{36}$/.test(body.source_attempt_id) || !Number.isInteger(body.source_version)
    || body.source_version < 0 || raw.length > 70000
    || (body.reprepare_handler !== undefined && (body.action !== 'rerun' || !body.reprepare_handler
      || typeof body.reprepare_handler.key !== 'string' || !body.reprepare_handler.key
      || typeof body.reprepare_handler.version !== 'string' || !body.reprepare_handler.version
      || !body.parameters || typeof body.parameters !== 'object' || Array.isArray(body.parameters)))) throw new Error("本次提交记录无法读取，请保留记录并刷新页面核查。");
  return value;
}
