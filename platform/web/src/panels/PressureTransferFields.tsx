import { useEffect, useRef, useState } from 'react';
import type { PressureTransfer } from '../utils/executionLaunch';
import { jobRecoveryError, jobRecoveryRequest } from '../utils/jobRecovery';

type PressureSurface = { id: string; patch: string; time: number; field: string; mode: string; unit: string;
  reference_density_kg_m3?: number; nodal_loads?: { positions_m: number[][] } };
type PressureResult = { publication?: { id: string }; metrics: { extractions?: { surfaceIntegrals?: PressureSurface[] } } };
export const emptyPressureTransfer: PressureTransfer = { publication_id: '', surface: '', patch: '', time: 0, node_set: '' };

export default function PressureTransferFields({ caseId, value, onChange }: {
  caseId: number; value: PressureTransfer; onChange: (value: PressureTransfer) => void;
}) {
  const [surfaces, setSurfaces] = useState<PressureSurface[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  async function readSurfaces() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setError('');
    const identifier = value.publication_id.trim().toLowerCase();
    try {
      const filter = identifier ? `?${new URLSearchParams({ publication_id: identifier })}` : '';
      const result = await jobRecoveryRequest<PressureResult>(`/cases/${caseId}/postprocess/results${filter}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (!result.publication?.id || (identifier && identifier !== result.publication.id)) {
        setError('请选择可核验的后处理发布。');
        return;
      }
      const available = (result.metrics.extractions?.surfaceIntegrals ?? []).filter(row =>
        row.mode === 'pressureForce' && row.unit === 'N'
        && (row.field === 'p' || row.field === 'pa' && row.reference_density_kg_m3 === undefined));
      setSurfaces(available);
      const selected = available.length === 1 ? available[0]
        : available.find(row => row.id === value.surface && row.patch === value.patch && row.time === value.time);
      onChange({ ...value, publication_id: result.publication.id, surface: selected?.id ?? '',
        patch: selected?.patch ?? '', time: selected?.time ?? 0,
        distribution: selected?.nodal_loads ? value.distribution : undefined });
      if (!available.length) setError('此发布没有以牛顿输出的压力力积分。');
    } catch (failure) {
      if (!controller.signal.aborted) setError(jobRecoveryError(failure));
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const selected = surfaces.findIndex(row => row.id === value.surface && row.patch === value.patch && row.time === value.time);
  return <fieldset className="execution-wide" aria-label="OpenFOAM 压力载荷" disabled={busy}>
    <label className="execution-wide">后处理发布 ID
      <input maxLength={36} value={value.publication_id} onChange={event => {
        setSurfaces([]); setError(''); onChange({ ...emptyPressureTransfer, node_set: value.node_set, publication_id: event.target.value });
      }} />
    </label>
    <button type="button" className="execution-wide" onClick={() => void readSurfaces()}>
      {busy ? '正在读取压力结果…' : value.publication_id.trim() ? '读取指定发布' : '读取当前后处理发布'}
    </button>
    {surfaces.length > 0 && <label className="execution-wide">压力曲面积分
      <select value={selected < 0 ? '' : selected} onChange={event => {
        const row = event.target.value ? surfaces[Number(event.target.value)] : undefined;
        onChange({ ...value, surface: row?.id ?? '', patch: row?.patch ?? '', time: row?.time ?? 0,
          distribution: row?.nodal_loads ? value.distribution : undefined });
      }}>
        <option value="">选择边界与时刻</option>
        {surfaces.map((row, index) => <option key={`${row.id}:${row.patch}:${row.time}`} value={index}>
          {row.id} · {row.patch} · {row.time} s{row.field === 'pa' ? ' · 声压 pa' : ''}
        </option>)}
      </select>
    </label>}
    {!surfaces.length && value.surface && <p className="execution-wide">{value.surface} · {value.patch} · {value.time} s</p>}
    <label className="execution-wide">CalculiX 节点集
      <input maxLength={80} value={value.node_set} onChange={event => onChange({ ...value, node_set: event.target.value })} />
    </label>
    {(surfaces[selected]?.nodal_loads || value.distribution === 'conformalNodal') &&
      <label className="execution-wide">载荷分配
        <select value={value.distribution ?? 'uniformResultant'} onChange={event => onChange({ ...value,
          distribution: event.target.value === 'conformalNodal' ? 'conformalNodal' : undefined })}>
          <option value="uniformResultant">均分压力合力</option>
          <option value="conformalNodal">按对应节点传递</option>
        </select>
      </label>}
    <p className="execution-wide execution-muted">{value.distribution === 'conformalNodal'
      ? '源边界点与目标节点须在同一米制坐标系中一一对应。' : '将压力合力均分到目标节点集。'}</p>
    {error && <p role="alert" className="execution-wide">{error}</p>}
  </fieldset>;
}
