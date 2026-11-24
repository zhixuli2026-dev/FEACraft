import { checkedPublication } from './publicationStatus.ts';

export interface ReportPublication {
  id: string;
  generation: number;
  status: string;
  current: boolean;
  stale: boolean;
}

export interface DocumentSnapshot {
  caseId: number;
  path: string;
  original: Blob;
  publication: ReportPublication | null;
}

async function request(url: string, signal?: AbortSignal) {
  const token = localStorage.getItem("vf_token");
  return fetch(url, { signal, headers: token ? { Authorization: `Bearer ${token}` } : {} });
}

export async function fetchDocumentFile(caseId: number, path: string, signal?: AbortSignal) {
  const response = await request(`/api/v1/cases/${caseId}/raw?path=${encodeURIComponent(path)}`, signal);
  if (!response.ok) throw new Error(`${path} 读取失败 (${response.status})`);
  return response;
}

/** Pin the published report once; downloads reuse the exact bytes rendered. */
export async function readDocumentSnapshot(caseId: number, path: string, signal?: AbortSignal,
    publicationId?: string): Promise<DocumentSnapshot> {
  let publication: ReportPublication | null = null;
  let response: Response;
  if (path === "E-report/build/report.html" || path === "E-report/build/report.docx") {
    const selected = await request(`/api/v1/cases/${caseId}/report/results`
      + (publicationId ? `?publication_id=${encodeURIComponent(publicationId)}` : ""), signal);
    if (selected.ok) {
      const document = await selected.json();
      const candidate = checkedPublication(document.publication, publicationId);
      publication = candidate;
      response = await request(`/api/v1/cases/${caseId}/report/artifact?path=${encodeURIComponent(path)}`
        + `&publication_id=${encodeURIComponent(candidate.id)}`, signal);
      // A failed pinned read must never silently fall back to a mutable alias.
      if (!response.ok) throw new Error(`报告版本 ${candidate.generation} 读取失败 (${response.status})`);
    } else if (selected.status === 404 && !publicationId) {
      // No active published report: an ordinary workspace document stays readable.
      response = await fetchDocumentFile(caseId, path, signal);
    } else {
      throw new Error(`无法核验报告版本 (${selected.status})，请重新读取`);
    }
  } else {
    if (publicationId) throw new Error("此文件不支持报告版本选择");
    response = await fetchDocumentFile(caseId, path, signal);
  }
  return { caseId, path, publication, original: await response.blob() };
}
