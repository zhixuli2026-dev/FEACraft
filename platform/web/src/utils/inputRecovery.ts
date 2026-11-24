export interface InputRecoveryReceipt {
  id: string; source_job_id: number; target_job_id: number; case_id: number; actor_user_id: number;
  source_attempt_id: string; target_attempt_id: string; note: string; recorded_at: string;
  original_inputs_reconstructed: false; old_execution_replayed: false; stop_evidence_created: false;
  new_execution_requested: true;
}
export interface InputRecoveryPlan {
  job_id: number; case_id: number; available: boolean; reason: { code: string; message: string } | null;
  source_attempt_id: string | null; source_snapshot_sha256: string;
  expected_revision: number; expected_event_sequence: number; command_available: boolean;
  handler_choices: { key: string; version: string }[]; links: InputRecoveryReceipt[];
  workflow: { run_id: string; step_id: string; step_key: string } | null;
}
export interface InputRecoveryBody {
  action: 'reprepare_current_inputs'; source_attempt_id: string; source_snapshot_sha256: string;
  expected_revision: number; expected_event_sequence: number;
  handler: { key: string; version: string } | null; parameters: Record<string, unknown> | null;
  command: string | null; runtime: 'windows' | 'wsl' | 'wsl-sandbox'; input_paths: string[] | null;
  timeout_seconds: number; max_log_bytes: number; note: string;
}
export interface PendingInputRecovery { schema: 1; jobId: number; key: string; body: InputRecoveryBody }
export const inputRecoveryKey = (userId: number, caseId: number, jobId: number) => `vf-input-recovery-v1:${userId}:${caseId}:${jobId}`;
export function readPendingInputRecovery(key: string, jobId: number): PendingInputRecovery | null {
  const raw = sessionStorage.getItem(key);
  if (!raw) return null;
  const value = JSON.parse(raw) as PendingInputRecovery;
  const body = value?.body;
  if (raw.length > 70000 || value.schema !== 1 || value.jobId !== jobId || !/^[0-9a-f-]{36}$/.test(value.key)
    || !body || body.action !== 'reprepare_current_inputs' || !/^[0-9a-f-]{36}$/.test(body.source_attempt_id)
    || !/^[0-9a-f]{64}$/.test(body.source_snapshot_sha256) || !Number.isSafeInteger(body.expected_revision)
    || body.expected_revision < 0 || !Number.isSafeInteger(body.expected_event_sequence) || body.expected_event_sequence < 1
    || !['windows', 'wsl', 'wsl-sandbox'].includes(body.runtime)
    || !Number.isFinite(body.timeout_seconds) || body.timeout_seconds <= 0 || body.timeout_seconds > 86400
    || !Number.isSafeInteger(body.max_log_bytes) || body.max_log_bytes < 1024 || body.max_log_bytes > 64 * 1024 * 1024
    || typeof body.note !== 'string' || body.note.length > 2000 || body.note.includes('\0')
    || (body.handler === null ? typeof body.command !== 'string' || !body.command || body.parameters !== null
      : !body.handler || typeof body.handler.key !== 'string' || typeof body.handler.version !== 'string'
        || !body.parameters || typeof body.parameters !== 'object' || Array.isArray(body.parameters) || body.command !== null)
    || (body.input_paths !== null && (!Array.isArray(body.input_paths) || !body.input_paths.length
      || body.input_paths.length > 256 || body.input_paths.some(path => typeof path !== 'string' || !path)))) {
    throw new Error('本机输入恢复记录无法核验');
  }
  return value;
}
