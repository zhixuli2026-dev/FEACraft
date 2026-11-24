import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import type { ReportPublication } from '../utils/documentSnapshot';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import { checkedPublication } from '../utils/publicationStatus';
import { saveDerivative } from '../utils/reportDerivative';
import './JobPostprocessEvidence.css';

type Artifact = { path: string; size: number; sha256: string };
type Chart = { id: string; title?: string; artifacts?: string[] };
type Metric = { name: string; time?: number; unit?: string; value: unknown; patch?: string; mode?: string };
type Mode = { id: string; field?: string; rank?: number; cell_count?: number; spatial_artifact?: string };
type Comparison = {
  metric: string; status: string; l2_relative?: number; reference_id?: string;
  reference?: { id?: string; kind?: string; source?: string; l2_max?: unknown };
};
type Evidence = {
  job_id: number; result_dir: string; publication?: ReportPublication;
  manifest?: { artifacts?: Artifact[] };
  metrics: {
    case_id?: number;
    readiness?: { report_ready?: boolean; blockers?: string[]; can_claim_verification?: boolean; can_claim_validation?: boolean };
    metrics?: Metric[]; comparisons?: Comparison[]; extractions?: { charts?: Chart[]; modalQuantities?: Mode[] };
  };
};
const figureName = /^[A-Za-z0-9_-]{1,80}\.(?:png|pdf|svg|plot\.csv)$/;
const meshName = /^[A-Za-z0-9_-]{1,80}\.vtu$/;
const kinds: Record<string, string> = { verification: 'Verification', validation: 'Validation', regression: '回归' };
const checks: Record<string, string> = { pass: '通过', fail: '未通过', invalid_reference: '参考无效',
  unit_mismatch: '单位不匹配', missing_metric: '缺少指标', missing_provenance: '缺少来源', sampling_mismatch: '采样不匹配' };
const surfaceModes: Record<string, string> = { areaIntegrate: '面积分', normalFlux: '法向通量', pressureForce: '压力合力' };

function items<T>(value: T[] | undefined): T[] { return Array.isArray(value) ? value : []; }

function valueText(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (Array.isArray(value)) return JSON.stringify(value);
  if (typeof value === 'string' && value.trim()) return value;
  return '—';
}

function failureText(failure: unknown) {
  if (failure instanceof ApiError) {
    if (failure.status === 401) return '请重新登录后读取后处理证据。';
    if ([403, 404].includes(failure.status)) return '此作业的证据不可访问，请核对算例权限或发布版本。';
    if (failure.code === 'evidence_invalid') return '归档证据无法核验，请核对所选版本。';
    if (failure.status === 409 || failure.status === 429) return '算例证据正在变化或读取繁忙，请稍后重试。';
  }
  return failure instanceof Error && !(failure instanceof TypeError)
    ? failure.message : '后处理证据读取失败，请重试。';
}

export default function JobPostprocessEvidence({ caseId, jobId, publicationId }: {
  caseId: number; jobId: number; publicationId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState<Evidence>();
  const [failure, setFailure] = useState('');
  const [fileFailure, setFileFailure] = useState('');
  const [busy, setBusy] = useState('');
  const [preview, setPreview] = useState<{ url: string; alt: string }>();
  const [metricLimit, setMetricLimit] = useState(12);
  const [comparisonLimit, setComparisonLimit] = useState(8);
  const transfer = useRef<AbortController | null>(null);
  const previewFrame = useRef<HTMLElement>(null);
  const previewTrigger = useRef<HTMLButtonElement | null>(null);
  const parent = `/cases/${caseId}`;
  const evidencePath = `${parent}/postprocess/results?${new URLSearchParams(
    publicationId ? { publication_id: publicationId } : { job_id: String(jobId) })}`;

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setResult(undefined); setFailure(''); setPreview(undefined); setFileFailure(''); setBusy('');
    void jobRecoveryRequest<Evidence>(evidencePath, {signal: controller.signal}).then(value => {
      if (controller.signal.aborted) return;
      if (value.job_id !== jobId || value.result_dir !== `D-post/data/postprocess-${jobId}`
        || !value.metrics || (value.metrics.case_id !== undefined && value.metrics.case_id !== caseId)) {
        throw new Error('证据与所选作业不一致，请重新选择。');
      }
      if (value.publication || publicationId) checkedPublication(value.publication, publicationId);
      setResult(value);
    }).catch(error => { if (!controller.signal.aborted) setFailure(failureText(error)); });
    return () => { controller.abort(); transfer.current?.abort(); transfer.current = null; };
  }, [open, retry, evidencePath, caseId, jobId, publicationId]);

  useEffect(() => {
    if (preview) previewFrame.current?.focus();
    return () => { if (preview) URL.revokeObjectURL(preview.url); };
  }, [preview]);

  async function readFile(file: Artifact, title: string, showPreview: boolean) {
    if (!result || transfer.current) return;
    const controller = new AbortController();
    transfer.current = controller;
    setBusy(file.path); setFileFailure('');
    if (showPreview) setPreview(undefined);
    try {
      const token = localStorage.getItem('vf_token');
      const query = new URLSearchParams({path: file.path});
      if (result.publication) query.set('publication_id', result.publication.id);
      const path = result.publication ? 'postprocess/artifact' : 'raw';
      const response = await fetch(`/api/v1${parent}/${path}?${query}`, {signal: controller.signal,
        headers: token ? { Authorization: `Bearer ${token}` } : {}});
      if (!response.ok) throw new ApiError(response.status, await response.json().then(body => body.detail).catch(() => null));
      const data = await response.arrayBuffer();
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)),
        byte => byte.toString(16).padStart(2, '0')).join('');
      if (controller.signal.aborted) return;
      if (data.byteLength !== file.size || hash !== file.sha256) throw new Error('图件与发布清单不一致。');
      if (showPreview) {
        setPreview({url: URL.createObjectURL(new Blob([data], {type: 'image/png'})), alt: title});
      } else saveDerivative(data, file.path);
    } catch (error) {
      if (!controller.signal.aborted) setFileFailure(failureText(error));
    } finally {
      if (transfer.current === controller) { transfer.current = null; setBusy(''); }
    }
  }

  const list = items(result?.metrics.metrics).filter(row => typeof row?.name === 'string');
  const comparisons = items(result?.metrics.comparisons).filter(row => typeof row?.metric === 'string');
  const readiness = result?.metrics.readiness;
  const blockers = items(readiness?.blockers).filter(item => typeof item === 'string');
  const publication = result?.publication;
  const decision = readiness?.report_ready === false ? '报告受阻' : readiness?.report_ready === true
    ? (publication && (!publication.current || publication.stale) ? '提取时就绪' : '报告就绪') : '缺少就绪记录';
  const files = items(result?.manifest?.artifacts).filter(file => file && typeof file.path === 'string'
    && Number.isSafeInteger(file.size) && file.size >= 0 && /^[a-f0-9]{64}$/.test(file.sha256));
  const plots = items(result?.metrics.extractions?.charts).filter(chart => typeof chart?.id === 'string').map(chart => {
    const names = items(chart.artifacts).filter(name => typeof name === 'string' && figureName.test(name));
    return { ...chart, files: files.filter(file => names.some(name => file.path === `${result?.result_dir}/${name}`)) };
  });
  const modes = items(result?.metrics.extractions?.modalQuantities).flatMap(mode => {
    if (typeof mode?.id !== 'string' || typeof mode.spatial_artifact !== 'string' || !meshName.test(mode.spatial_artifact)) return [];
    const file = files.find(value => value.path === `${result?.result_dir}/${mode.spatial_artifact}`);
    return [{ ...mode, file }];
  });

  return <details className="job-postprocess-evidence" data-testid="job-postprocess-evidence" onToggle={event => {
    const next = event.currentTarget.open;
    setOpen(next);
    if (!next) { setPreview(undefined); setResult(undefined); setFailure(''); }
  }}>
    <summary>后处理证据</summary>
    {open && <section aria-label="后处理证据" aria-busy={!result && !failure}>
      {failure ? <><p role="alert">{failure}</p><button type="button" onClick={() => setRetry(value => value + 1)}>重试读取证据</button></>
        : !result ? <p role="status">正在核验证据和发布版本…</p> : <>
          <div className="job-evidence-heading">
            <div><strong data-testid="evidence-readiness">{decision}</strong>
              <p className="job-evidence-secondary">{publication ? `第 ${publication.generation} 版 · ${!publication.current
                ? '历史发布' : publication.stale ? '当前发布 · 来源已过期' : '当前发布'}` : '旧版结果'}</p>
            </div>
            <button type="button" disabled={!!busy} onClick={() => setRetry(value => value + 1)}>刷新证据</button>
          </div>
          {blockers.length > 0 && <ul className="job-evidence-blockers" aria-label="报告阻碍">
            {blockers.map((reason, index) => <li key={`${index}:${reason}`}>{reason}</li>)}
          </ul>}
          {(readiness?.can_claim_verification !== undefined || readiness?.can_claim_validation !== undefined) &&
            <p className="job-evidence-secondary">Verification {readiness?.can_claim_verification ? '通过' : '未建立'} ·
              Validation {readiness?.can_claim_validation ? '通过' : '未建立'}</p>}
          <div className="job-evidence-tables"><div>{list.length ? <table>
            <caption>后处理指标</caption><thead><tr><th scope="col">指标</th><th scope="col">数值与单位</th></tr></thead>
            <tbody>{list.slice(0, metricLimit).map((row, index) => <tr key={`${row.name}:${index}`}>
              <th scope="row">{row.name}{typeof row.patch === 'string' && <span className="job-evidence-secondary job-evidence-source">
                {row.patch} · {surfaceModes[row.mode ?? 'areaIntegrate'] ?? '面积分'}</span>}</th><td>{valueText(row.value)}
                {typeof row.unit === 'string' && <> <span className="job-evidence-secondary">{row.unit}</span></>}</td>
            </tr>)}</tbody>
          </table> : <p>此发布没有标量指标。</p>}
          {list.length > metricLimit && <button type="button" onClick={() => setMetricLimit(value => value + 12)}>显示更多指标</button>}</div>
          {comparisons.length > 0 && <div><table>
            <caption>参考比较</caption><thead><tr><th scope="col">指标与来源</th><th scope="col">相对 L2 / 容差</th></tr></thead>
            <tbody>{comparisons.slice(0, comparisonLimit).map((row, index) => <tr key={`${row.metric}:${index}`}>
              <th scope="row">{row.metric}<span className="job-evidence-secondary job-evidence-source">
                {[row.reference_id ?? row.reference?.id, kinds[row.reference?.kind ?? ''], row.reference?.source].filter(Boolean).join(' · ')}</span></th>
              <td><strong>{checks[row.status] ?? row.status}</strong><br />{valueText(row.l2_relative)} / {
                valueText(row.reference?.l2_max ?? (['pass', 'fail'].includes(row.status) ? 0 : undefined))}</td>
            </tr>)}</tbody>
          </table>
          {comparisons.length > comparisonLimit && <button type="button" onClick={() => setComparisonLimit(value => value + 8)}>显示更多参考比较</button>}</div>}
          </div>
          {modes.length > 0 && <section className="job-evidence-charts" aria-label="全场模态">
            <h3>全场模态</h3><ul>{modes.map(mode => <li key={mode.id}>
              <span>{mode.id}<span className="job-evidence-secondary job-evidence-source">
                {mode.field} · {mode.rank} 个模态 · {mode.cell_count} 个单元</span></span>
              {mode.file ? <button type="button" aria-label={`下载 ${mode.id} VTU`} disabled={!!busy}
                onClick={() => void readFile(mode.file!, mode.id, false)}>下载 VTU</button>
                : <span className="job-evidence-secondary">此模态没有可核验的网格。</span>}
            </li>)}</ul>
          </section>}
          {plots.length > 0 && <section className="job-evidence-charts" aria-label="图件与数据">
            <h3>图件与数据</h3>
            <ul>{plots.map(plot => {
              const title = typeof plot.title === 'string' && plot.title ? plot.title : plot.id;
              const png = plot.files.find(file => file.path.endsWith('.png'));
              return <li key={plot.id}><span>{title}</span><div className="job-evidence-actions">
                {png && <button type="button" aria-label={`预览 ${title}`} disabled={!!busy}
                  onClick={event => { previewTrigger.current = event.currentTarget; void readFile(png, title, true); }}>预览</button>}
                {plot.files.map(file => {
                  const format = file.path.split('.').at(-1)?.toUpperCase();
                  return <button key={file.path} type="button" aria-label={`下载 ${title} ${format}`} disabled={!!busy}
                    onClick={() => void readFile(file, title, false)}>{format}</button>;
                })}
                {!plot.files.length && <span className="job-evidence-secondary">此图件没有可核验的下载文件。</span>}
              </div></li>;
            })}</ul>
          </section>}
          {busy && <p role="status">正在核验并读取图件…</p>}
          {fileFailure && <p role="alert">{fileFailure}</p>}
          {preview && <figure ref={previewFrame} tabIndex={0} aria-label="图件预览">
            <img src={preview.url} alt={preview.alt} /><figcaption>{preview.alt}
            <button type="button" onClick={() => { setPreview(undefined); previewTrigger.current?.focus(); }}>关闭预览</button></figcaption></figure>}
        </>}
    </section>}
  </details>;
}
