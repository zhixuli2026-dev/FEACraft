import { useEffect, useState } from 'react';
import { ApiError } from '../api.ts';
import type { ReportPublication } from './documentSnapshot';

export function checkedPublication(value: unknown, expectedId?: string): ReportPublication {
  const row = value as ReportPublication | null;
  if (!row || typeof row.id !== 'string' || !row.id || (expectedId && row.id !== expectedId)
    || !Number.isSafeInteger(row.generation) || row.generation < 1
    || !['committed', 'superseded'].includes(row.status)
    || typeof row.current !== 'boolean' || typeof row.stale !== 'boolean') throw new Error('发布版本信息无法核验，请重新选择。');
  return row;
}
export function usePublicationStatus(userId: number | undefined, caseId: number | undefined,
    channel: string | null, initial: ReportPublication | null) {
  const id = initial?.id, key = `${userId}:${caseId}:${channel}:${id}`;
  const [state, setState] = useState<{ key: string; publication: ReportPublication; error: string; blocked: boolean } | null>(null);
  useEffect(() => {
    if (!userId || !caseId || !channel || !id || !initial) return;
    const controller = new AbortController(); let inFlight = false;
    const load = async () => {
      if (inFlight || controller.signal.aborted) return;
      inFlight = true;
      try {
        const path = channel === 'derivative' ? `report/derivative?publication_id=${encodeURIComponent(id)}`
          : channel === 'report' ? `report/results?publication_id=${encodeURIComponent(id)}`
          : `latex/results?main=${encodeURIComponent(channel.slice(6))}&publication_id=${encodeURIComponent(id)}`;
        const token = localStorage.getItem('vf_token');
        const response = await fetch(`/api/v1/cases/${caseId}/${path}`,
          { signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!response.ok) throw new ApiError(response.status, '所选发布版本暂时无法核验');
        const document = await response.json(), publication = checkedPublication(document.publication, id);
        if (channel.startsWith('latex:') && document.manifest?.main !== channel.slice(6)) throw new Error('发布不属于所选主文件。');
        if (!controller.signal.aborted) setState({ key, publication, error: '', blocked: false });
      } catch (failure) {
        if (!controller.signal.aborted) {
          const blocked = failure instanceof ApiError ? [401, 403, 404, 409, 422].includes(failure.status) : !(failure instanceof TypeError);
          setState(previous => ({ key, publication: previous?.key === key ? previous.publication : initial,
            error: blocked ? '所选版本已不存在、损坏或无权访问。请核对原版本或明确选择其他发布。'
              : '版本状态暂时无法刷新，正在显示最近核验的固定版本。', blocked }));
        }
      } finally { inFlight = false; }
    };
    void load(); const timer = window.setInterval(() => void load(), 3000);
    const reconcile = (event: Event) => { if ((event as CustomEvent).detail?.caseId === caseId) void load(); };
    window.addEventListener('vibeflow:case-reconciled', reconcile);
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener('vibeflow:case-reconciled', reconcile); };
  }, [userId, caseId, channel, id, key, initial]);
  return state?.key === key ? state : { publication: initial, error: '', blocked: false };
}
export function publicationLabel(publication: ReportPublication) {
  return `版本 ${publication.generation} · ${publication.status === 'superseded' ? '保留产物，未成为当前有效结果' : !publication.current ? '历史发布，不代表当前有效结果'
    : publication.stale ? '当前发布已过期，保留供查阅' : '当前发布，最近已核验'} · 阅读已固定此版本`;
}
