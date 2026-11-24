import type { ReportPublication } from "./documentSnapshot";
import { checkedPublication } from './publicationStatus.ts';

export function latexMainForPdf(path: string): string | null {
  const match = /^(.*\/)?build\/([^/]+)\.pdf$/.exec(path);
  return match ? `${match[1] ?? ""}${match[2]}.tex` : null;
}

export interface PdfSelection { url: string; publication: ReportPublication | null; main: string | null }

export async function pdfRequest(url: string, signal?: AbortSignal, version?: string) {
  const token = localStorage.getItem("vf_token");
  return fetch(url, {signal, headers: {...(token ? {Authorization: `Bearer ${token}`} : {}),
    ...(version ? {"X-Vibeflow-File-Version": version} : {})}});
}

/** Resolve once; every range/download/source lookup keeps this publication. */
export async function selectPdf(caseId: number, path: string, signal?: AbortSignal,
    publicationId?: string): Promise<PdfSelection> {
  const main = latexMainForPdf(path);
  const prefix = `/api/v1/cases/${caseId}`;
  if (main) {
    const response = await pdfRequest(`${prefix}/latex/results?main=${encodeURIComponent(main)}`
      + (publicationId ? `&publication_id=${encodeURIComponent(publicationId)}` : ""), signal);
    if (response.ok) {
      const document = await response.json(), publication = checkedPublication(document.publication, publicationId);
      if (document.pdf !== path || document.manifest?.main !== main) throw new Error("PDF 发布版本信息无效");
      return {main, publication, url: `${prefix}/latex/artifact?path=${encodeURIComponent(path)}`
        + `&publication_id=${encodeURIComponent(publication.id)}`};
    }
    if (response.status !== 404 || publicationId) throw new Error(`无法核验 PDF 发布版本 (${response.status})`);
  } else if (publicationId) throw new Error("此 PDF 不属于可选择的 LaTeX 发布");
  return {main, publication: null, url: `${prefix}/raw?path=${encodeURIComponent(path)}`};
}
