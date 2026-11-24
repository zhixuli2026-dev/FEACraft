import { checkedPublication } from './publicationStatus';

export interface DerivativeFile { path: string; size: number; sha256: string; role: "original" | "previous-draft" }
export interface Derivative {
  family: "draft" | "ppt" | "pdf-number"; output: string;
  publication: { id: string; generation: number; status: string; current: boolean; stale: boolean; applied?: boolean };
  files: DerivativeFile[];
}

async function request(url: string, signal?: AbortSignal) {
  const token = localStorage.getItem("vf_token");
  const response = await fetch(url, {signal, headers: token ? {Authorization:`Bearer ${token}`} : {}});
  if (!response.ok) throw new Error(`报告版本读取失败 (${response.status})`);
  return response;
}

export async function readDerivative(caseId: number, id: string, signal?: AbortSignal): Promise<Derivative> {
  const response = await request(`/api/v1/cases/${caseId}/report/derivative?publication_id=${encodeURIComponent(id)}`,signal);
  const value = await response.json();
  if (value?.publication?.id !== id || !Number.isInteger(value.publication.generation)
      || typeof value.publication.current !== "boolean" || typeof value.publication.stale !== "boolean"
      || !["draft","ppt","pdf-number"].includes(value.family) || typeof value.output !== "string"
      || !Array.isArray(value.files) || !value.files.length || value.files.some((file: DerivativeFile) =>
        typeof file.path !== "string" || !Number.isSafeInteger(file.size) || file.size < 0
        || !/^[0-9a-f]{64}$/.test(file.sha256) || !["original","previous-draft"].includes(file.role))) {
    throw new Error("报告版本信息不完整，请重新选择。");
  }
  checkedPublication(value.publication, id);
  return value;
}

export async function readDerivativeFile(caseId: number, id: string, file: DerivativeFile, signal?: AbortSignal) {
  const response = await request(`/api/v1/cases/${caseId}/report/derivative-artifact?publication_id=${encodeURIComponent(id)}`
    + `&path=${encodeURIComponent(file.path)}`,signal);
  const data = await response.arrayBuffer();
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",data)),b=>b.toString(16).padStart(2,"0")).join("");
  if (data.byteLength !== file.size || digest !== file.sha256) throw new Error("报告原件与所选版本不一致，请重新读取。");
  return data;
}

export function saveDerivative(data: ArrayBuffer, name: string) {
  const url=URL.createObjectURL(new Blob([data]));
  const anchor=document.createElement("a");anchor.href=url;anchor.download=name.split("/").pop() || "report";
  document.body.appendChild(anchor);anchor.click();anchor.remove();
  window.setTimeout(()=>URL.revokeObjectURL(url),1000);
}

