import { ApiError } from '../api';

export type NativeKind = 'terminal' | 'cli' | 'viz';
export interface SessionHistoryRow {
  kind: NativeKind; id: string; case_id: number | null; phase: string; original_phase: string;
  version: number; runtime: string; created_at: string; ended_at: string | null;
  error: string | null; cleanup_verified: boolean | null; owner_available: boolean;
  alive: boolean | null; issues: string[]; conversation_id: number | null;
}
export interface SessionReview {
  id: string; state: string; snapshot_sha256: string; recorded_at: string;
  note: string; issues: string[]; original_phase: string;
  execution_authorized: false; stop_evidence_created: false;
}
export interface SessionHistoryPlan extends SessionHistoryRow {
  snapshot_sha256: string; available_actions: string[]; review: SessionReview | null; reviews: SessionReview[];
  links?: SessionLink[];
}
export interface SessionLink {
  id: string; kind: NativeKind; source_id: string; successor_id: string; recorded_at: string; note: string;
  source_snapshot_sha256: string; successor_identity_sha256: string;
  execution_authorized: false; stop_evidence_created: false; context_inherited: false;
}
export interface SessionLinkPlan {
  source: SessionHistoryRow; successor: SessionHistoryRow; source_snapshot_sha256: string;
  successor_identity_sha256: string; existing: SessionLink | null;
}
export const nativeSessionClosedEvent = 'vibeflow:native-session-closed';
export type NativeSessionClosed = Pick<SessionHistoryRow, 'kind' | 'id' | 'case_id'> & { userId: number };
export const kindNames: Record<NativeKind, string> = { terminal: '终端', cli: 'CLI', viz: '可视化' };
export const sessionPhase = (phase: string) => ({ starting: '启动中', active: '运行中', closed: '已关闭', failed: '异常结束',
  recovery_required: '需要恢复核查', evidence_invalid: '记录无法核验' } as Record<string, string>)[phase] ?? '状态待核查';
export const sessionIssue = (issue: string) => ({ session_evidence_invalid: '原记录无法完整核验，请保留文件和原执行主机记录。',
  session_owner_unavailable: '当前服务无法连接原执行者；这不代表原进程已经退出。',
  session_cleanup_unverified: '完整停止与读写结束尚未核验。',
  terminal_state_not_stop_proof: '关闭或失败状态不足以证明原执行范围已经停止。' } as Record<string, string>)[issue] ?? '原会话仍有待核查事项。';

export async function sessionHistoryRequest<T>(path: string, options: { signal?: AbortSignal; body?: unknown; method?: 'DELETE' } = {}): Promise<T> {
  const token = localStorage.getItem('vf_token');
  const headers: Record<string, string> = token ? { Authorization: 'Bearer ' + token } : {};
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/v1' + path, { method: options.method ?? (options.body === undefined ? 'GET' : 'POST'), headers,
    credentials: 'same-origin', signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body) });
  if (!response.ok) {
    let detail: unknown = '请求未完成';
    try { detail = (await response.json()).detail ?? detail; } catch { /* Plain errors have no public detail. */ }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}
export function sessionHistoryError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return '登录已失效，请重新登录后读取会话记录。';
    if ([403, 404].includes(error.status)) return '当前账户无法操作该会话，请核对所属算例及权限。';
    if (error.code === 'state_conflict') return '原记录或核查说明已变化，请刷新方案核对后再确认。';
    if ([400, 422].includes(error.status)) return '确认参数未通过校验，请检查说明并重新读取方案。';
    if (error.code === 'session_review_unverified') return '核查凭证无法完整核验，请保留现有记录并联系部署管理员。';
    if (error.code === 'session_lineage_conflict') return '后续会话须晚于原会话、属于同一算例且没有其他来源。请核对已有关系后重新选择。';
    if (error.code === 'session_lineage_unverified') return '会话登记或关联凭证无法核验，请保留原记录后重新读取。';
    if (error.status === 409) return '核验条件尚未满足，请刷新原记录；原停止限制和占用继续保留。';
  }
  return '操作结果尚未确认，请刷新记录核对原回执，勿另建会话或重复发送任务。';
}
