const API = "/api/v1";

let token: string | null = localStorage.getItem("vf_token");

export class ApiError extends Error {
  status: number;
  code?: string;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    const value = detail as { message?: string; code?: string } | null;
    const message = typeof detail === "string" ? detail : value?.message ?? JSON.stringify(detail);
    super(`${status}: ${message}`);
    this.name = "ApiError";
    this.status = status;
    this.code = value?.code;
    this.detail = detail;
  }
}

export function setToken(t: string | null) {
  token = t;
  if (t) localStorage.setItem("vf_token", t);
  else localStorage.removeItem("vf_token");
}

async function req<T = any>(
  method: string,
  path: string,
  body?: unknown,
  mode: "json" | "form" | "bytes" = "json",
  idempotencyKey?: string,
  signal?: AbortSignal
): Promise<T> {
  const headers: Record<string, string> = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body !== undefined) {
    if (mode === "form") {
      payload = body as FormData;
    } else if (mode === "bytes") {
      headers["Content-Type"] = "application/octet-stream";
      payload = body as Blob;
    } else {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
  }
  const res = await fetch(`${API}${path}`, { method, headers, body: payload, signal });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const api = {
  get: <T = any>(p: string, key?:string, signal?:AbortSignal) => req<T>("GET", p, undefined, "json", key, signal),
  post: <T = any>(p: string, b?: unknown, key?:string, signal?:AbortSignal) => req<T>("POST", p, b, "json", key, signal),
  put: <T = any>(p: string, b?: unknown) => req<T>("PUT", p, b),
  patch: <T = any>(p: string, b?: unknown) => req<T>("PATCH", p, b),
  del: <T = any>(p: string, key?:string) => req<T>("DELETE", p, undefined, "json", key),
  upload: <T = any>(p: string, f: FormData) => req<T>("POST", p, f, "form"),
  putBytes: <T = any>(p: string, b: Blob, signal?: AbortSignal) => req<T>("PUT", p, b, "bytes", undefined, signal),
};

// ---- 类型 ----
export interface UserInfo { id: number; email: string; name: string }
export type AccessRole = 'owner' | 'member' | 'viewer';
export interface Workspace {
  id: number; name: string; created_at: string; shared?: boolean;
  access_role?: AccessRole | 'editor' | null; case_only?: boolean;
}
export interface CaseInfo {
  id: number; name: string; workflow_state: string; dict_revision: number;
  updated_at: string; access_role?: AccessRole;
}
export function isCaseReadOnly(currentCase: CaseInfo | null, workspace: Workspace | null) {
  const role = currentCase?.access_role ?? workspace?.access_role;
  return role === 'viewer' || (workspace?.case_only === true && role == null);
}
export interface TreeEntry { path: string; size: number; modified: string; kind?: "file" | "directory" }


/** Continue against the same append-only event history as the first page. */
export function olderConversationPage(cid: number, window?: {
  has_more: boolean; next_offset?: number | null; snapshot_id?: number;
}): string | null {
  if (!window?.has_more || !window.next_offset) return null;
  const query = new URLSearchParams({offset: String(window.next_offset), limit: "280"});
  if (window.snapshot_id !== undefined) query.set("snapshot_id", String(window.snapshot_id));
  return `/conversations/${cid}?${query}`;
}
