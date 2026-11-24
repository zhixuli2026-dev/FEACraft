import { ApiError } from "../api";

export interface RecoverySession {
  id: string; shell: string; case_id: number | null; phase: string;
  alive: boolean | null; cleanup_verified: boolean | null;
}
export interface RecoveryInspection { status: string; reason: string }
export interface RecoveryRecord {
  id: string; phase: string; error: string | null; cleanup_verified: boolean | null;
  created_at: string; ended_at: string | null;
}
export interface RecoveryPage { recoveries: RecoveryRecord[]; next_before: string | null }
export interface RecoveryDetail extends RecoveryRecord {
  receipt?: { archive_bytes: number; archive_sha256: string };
  manifest?: {
    files: Record<string, { size: number; sha256: string }>;
    directories: string[]; modes: Record<string, number>; bytes: number; entries: number;
  };
}

export const recoveryPath = (session: string) => `/terminal/sessions/${encodeURIComponent(session)}/recoveries`;

async function response(path: string, method = "GET", signal?: AbortSignal, body?: unknown) {
  const token = localStorage.getItem("vf_token");
  const result = await fetch("/api/v1" + path, {
    method, signal, cache: "no-store", headers: {
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!result.ok) {
    let detail: unknown = result.statusText;
    try { const body = await result.json(); detail = body.detail ?? body; } catch { /* Keep HTTP failure. */ }
    throw new ApiError(result.status, detail);
  }
  return result;
}

export async function recoveryRequest<T>(path: string, signal?: AbortSignal, method = "GET", body?: unknown): Promise<T> {
  return (await response(path, method, signal, body)).json();
}

const reasons: Record<string, string> = {
  image_cleanup_evidence_invalid: "镜像清理记录或原镜像状态发生变化，请保留现场并核对后重试。",
  image_cleanup_incomplete: "镜像清理尚未完成。请确认保留快照可用、相关进程已退出且镜像未被占用，再重试同一次清理。",
  image_cleanup_started: "原镜像已进入清理流程，无法再创建快照。请使用已保留的快照。",
  staging_cleanup_evidence_invalid: "暂存清理记录或目录状态发生变化，请保留现场并核对后重试。",
  staging_cleanup_incomplete: "清理尚未完成；未知或变化的文件会被保留。请核对快照、暂存内容和目录权限后重试。",
  retirement_busy: "其他收尾正在核验，请稍后重试。",
  retirement_evidence_invalid: "收尾记录或保留快照未通过核验，请检查原运行环境后重试。",
  retirement_already_recorded: "旧会话已有收尾记录，请查询已有结果。",
  retirement_binding_changed: "旧记录的收尾状态已变化，请刷新查询。",
  session_already_closed: "会话已正常关闭，无需再次收尾。",
  recovery_plan_stale: "项目文件在预览后已变化。请重新预览，再确认需要写回的内容。",
  recovery_protected_path: "caseDict 需要通过专用编辑流程恢复，请取消选择该文件。",
  recovery_destination_type: "目标路径已是目录，暂时不能按文件内容覆盖。",
  recovery_request_conflict: "这次确认标识与已有操作不同，请重新核对写回记录。",
  recovery_restore_unavailable: "暂时无法完整核验写回操作，请核对结果后重试。",
  owner_still_active: "原会话仍在运行。请先正常关闭终端，再刷新核验。",
  process_still_active: "原终端仍有进程在退出，请稍后刷新核验。",
  owner_identity_unverified: "暂时无法确认原会话的状态。请恢复原运行环境后重新核验。",
  process_exit_unverified: "暂时无法确认所有原进程已退出，请稍后刷新核验。",
  terminal_process_registration_incomplete: "原会话的启动记录不完整，目前无法恢复。",
  process_registration_incomplete: "原会话的进程记录不完整，目前无法恢复。",
  persistent_sandbox_required: "这条历史会话没有可用的持久沙箱副本。",
  case_source_changed: "项目的文件来源已改变。请先核对项目，再尝试恢复。",
  image_storage_binding_changed: "当前环境无法访问原沙箱副本，请在原运行环境重试。",
  records_changed_during_inspection: "会话状态正在变化，请刷新核验。",
  recovery_source_registration_changed: "原会话记录已变化，请刷新核验后重试。",
  inspection_timeout: "核验用时过长，请检查运行环境后重试。",
  inspection_busy: "其他核验正在进行，请稍后重试。",
  recovery_busy: "其他恢复正在进行，请稍后重试。",
  case_unavailable: "当前项目不可用，请先检查项目访问权限或回收站。",
  node_storage_full: "恢复所需的磁盘空间不足。请清理已确认可删除的数据或扩容后重试。",
  node_storage_unavailable: "暂时无法核对可用磁盘空间，请稍后重试。",
  sandbox_recovery_incomplete: "快照未能完成。请刷新记录查看结果，原沙箱副本已保留。",
  recovery_evidence_invalid: "快照文件或记录未通过校验，暂时不能读取。请刷新记录后重试。",
  recovery_evidence_changed: "快照记录刚刚发生变化，请刷新后重新下载。",
  recovery_not_complete: "这份快照尚未完成，请刷新记录查看状态。",
};

export function recoveryReason(reason: string) {
  return reasons[reason] ?? "暂时无法确认恢复条件，请刷新核验；持续失败时请检查原运行环境。";
}

export function recoveryError(error: unknown, creating = false) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后查看恢复记录。";
    if (error.status === 403 || error.status === 404) return "当前会话或快照已不可访问，请刷新会话列表并检查项目权限。";
    return error.code ? recoveryReason(error.code) : `请求失败（${error.status}），请刷新后重试。`;
  }
  return creating
    ? "连接中断，创建结果尚未确认。请刷新记录查看结果，避免重复创建。"
    : "读取失败，请检查连接后重试。";
}

export function recoveryPhase(phase: string) {
  return ({
    active: "正在生成", starting: "正在启动", succeeded: "可下载", failed: "生成失败",
    cancelled: "已取消", recovery_required: "执行中断，待核验", closed: "已关闭",
    evidence_invalid: "记录待核验",
  } as Record<string, string>)[phase] ?? "状态待核验";
}

export function recoveryDate(value: string) {
  const date = new Date(/(?:Z|[+-]\d\d:\d\d)$/i.test(value) ? value : value + "Z");
  return Number.isNaN(date.valueOf()) ? "时间未知" : date.toLocaleString("zh-CN", { hour12: false });
}

export function recoverySize(size: number) {
  return size < 1024 ? `${size} B` : size < 1024 * 1024
    ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(1)} MiB`;
}

export async function recoveryDownload(session: string, id: string, signal: AbortSignal, progress: (bytes: number, total: number) => void) {
  const path = recoveryPath(session) + "/" + encodeURIComponent(id);
  const detail = await recoveryRequest<RecoveryDetail>(path, signal);
  const receipt = detail.receipt;
  if (detail.phase !== "succeeded" || detail.cleanup_verified !== true || !receipt
      || !Number.isSafeInteger(receipt.archive_bytes) || receipt.archive_bytes <= 0
      || receipt.archive_bytes > 136 * 1024 * 1024 || !/^[a-f0-9]{64}$/.test(receipt.archive_sha256)) {
    throw new ApiError(409, { code: "recovery_evidence_invalid" });
  }
  const result = await response(path + "/download", "GET", signal);
  if (!result.body) throw new Error("Missing download body");
  const reader = result.body.getReader();
  const chunks: ArrayBuffer[] = [];
  let count = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      count += next.value.byteLength;
      if (count > receipt.archive_bytes) throw new ApiError(409, { code: "recovery_evidence_invalid" });
      chunks.push(new Uint8Array(next.value).buffer);
      progress(count, receipt.archive_bytes);
    }
    if (count !== receipt.archive_bytes) throw new ApiError(409, { code: "recovery_evidence_invalid" });
    const blob = new Blob(chunks, { type: "application/zip" });
    const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
    const hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    if (hash !== receipt.archive_sha256) throw new ApiError(409, { code: "recovery_evidence_invalid" });
    return blob;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
