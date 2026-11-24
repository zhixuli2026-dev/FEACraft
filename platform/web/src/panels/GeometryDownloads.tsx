import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { checkedPublication, publicationLabel } from '../utils/publicationStatus';
import type { ReportPublication } from '../utils/documentSnapshot';

interface ExportFile { path: string; size: number; sha256: string }
interface ExportResult { publication: ReportPublication; files: ExportFile[] }

/** The mounted account/case owns both the immutable selection and its download. */
export default function GeometryDownloads({ userId, caseId }: { userId: number; caseId: number }) {
  const [result, setResult] = useState<ExportResult | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const active = useRef<AbortController | null>(null), live = useRef(true);
  const current = () => live.current && useStore.getState().user?.id === userId && useStore.getState().currentCase?.id === caseId;
  useEffect(() => { live.current = true; return () => { live.current = false; active.current?.abort(); }; }, []);
  async function request(path: string, controller: AbortController) {
    const token = localStorage.getItem('vf_token');
    const response = await fetch(`/api/v1/cases/${caseId}/geometry/${path}`, {
      signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
      let detail: unknown = '几何文件暂时无法读取';
      try { detail = (await response.json()).detail ?? detail; } catch { /* Preserve the HTTP failure. */ }
      throw new ApiError(response.status, detail);
    }
    return response;
  }
  async function perform(action: (controller: AbortController) => Promise<void>) {
    if (active.current || !current()) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setError(''); setNotice('');
    try { await action(controller); }
    catch (failure) { if (current() && !controller.signal.aborted) setError(failure instanceof Error ? failure.message : '读取失败，请重试原版本。'); }
    finally { if (active.current === controller) active.current = null; if (current()) setBusy(false); }
  }
  function load(latest: boolean) {
    return perform(async controller => {
      const expected = !latest ? result?.publication.id : undefined;
      const response = await request('results' + (expected ? `?publication_id=${encodeURIComponent(expected)}` : ''), controller);
      const value = await response.json(), publication = checkedPublication(value.publication, expected);
      if (!Array.isArray(value.files) || value.files.some((file: ExportFile) =>
        typeof file.path !== 'string' || !/^A-geo\/exports\/.+\.(step|stp|stl)$/i.test(file.path)
        || file.path.split('/').some(part => !part || part === '.' || part === '..')
        || !Number.isSafeInteger(file.size) || file.size < 0 || !/^[0-9a-f]{64}$/.test(file.sha256))) throw new Error('几何文件清单无法核验。');
      if (current()) setResult({ publication, files: value.files });
    });
  }
  function download(file: ExportFile) {
    if (!result) return;
    const publication = result.publication;
    return perform(async controller => {
      const response = await request(`artifact?publication_id=${encodeURIComponent(publication.id)}&path=${encodeURIComponent(file.path)}`, controller);
      const blob = await response.blob();
      const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
      const actual = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
      if (blob.size !== file.size || actual !== file.sha256) throw new Error('下载内容与所选发布不一致，请重新核对原版本。');
      if (!current() || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = file.path.split('/').pop()!;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(`已开始下载版本 ${publication.generation} 的 ${anchor.download}`);
    });
  }
  return <details className="geo-downloads" onToggle={event => { if (event.currentTarget.open && !result && !active.current) void load(false); }}>
    <summary>已发布的 STEP / STL</summary>
    <section aria-label="几何发布文件" aria-busy={busy}>
      {result && <><p>{publicationLabel(result.publication)}</p><ul>{result.files.map(file => <li key={file.path}>
        <span>{file.path.split('/').pop()} · {(file.size / 1024).toFixed(1)} KiB</span>
        <button type="button" disabled={busy} onClick={() => void download(file)}>下载 {file.path.split('/').pop()}</button>
      </li>)}</ul>{!result.files.length && <p>此版本没有 STEP/STL 原件，请先执行几何导出。</p>}</>}
      <div className="geo-download-actions"><button type="button" disabled={busy} onClick={() => void load(false)}>重新核对{result ? '原版本' : '发布结果'}</button>
        {result && <button type="button" disabled={busy} onClick={() => void load(true)}>读取最新发布</button>}</div>
      {busy && <p role="status">正在读取并核验文件…</p>}
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    </section>
  </details>;
}
