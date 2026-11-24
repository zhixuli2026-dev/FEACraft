/** Navigation hints only. Every restored identity must be read through the API. */
export interface WorkspaceSelection { workspaceId: number; caseId: number | null }
export interface CaseSelection {
  section?: 'jobs' | 'workflows' | 'batches' | 'remote';
  batchId?: string | null;
  runId?: string | null;
  remoteRunId?: string | null;
  jobId?: number | null;
  attemptId?: string | null;
  caseRunId?: string | null;
  reading?: PublicationReading | null;
  previewTolerance?: number;
  cadTreeOpen?: boolean;
  cadAssembly?: boolean;
}
export interface PublicationReading { path: string; channel: string; id: string | null; invalid?: boolean }
export function publicationChannel(path: string): string | null {
  if (path.length > 2048 || path.startsWith('/') || path.includes('\\') || path.split('/').includes('..')) return null;
  if (['E-report/build/report.html', 'E-report/build/report.docx'].includes(path)) return 'report';
  const match = /^(.*\/)?build\/([^/]+)\.pdf$/.exec(path);
  return match ? `latex:${match[1] ?? ''}${match[2]}.tex` : null;
}
function reading(value: unknown): PublicationReading | null {
  const row = value as Partial<PublicationReading> | null;
  if (!row || typeof row.path !== 'string' || publicationChannel(row.path) !== row.channel || !row.channel) return null;
  const valid = row.id === null || identity(row.id);
  return { path: row.path, channel: row.channel, id: valid ? row.id! : null, invalid: !valid || row.invalid === true };
}
const positiveId = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
export const positivePreviewTolerance = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value > 0;
const identity = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 128;
const workspaceKey = (userId: number) => `vf-workspace-selection-v1:${userId}`;
const caseKey = (userId: number, caseId: number) => `vf-case-selection-v1:${userId}:${caseId}`;
function read(key: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? 'null');
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch { return {}; }
}
function write(key: string, value: object) {
  // A disabled/full browser store must never prevent navigation or submit a job.
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* Navigation still works in memory. */ }
}
export function readWorkspaceSelection(userId: number): WorkspaceSelection | null {
  const value = read(workspaceKey(userId));
  return positiveId(value.workspaceId) && (value.caseId === null || positiveId(value.caseId))
    ? { workspaceId: value.workspaceId, caseId: value.caseId } : null;
}
export function saveWorkspaceSelection(userId: number, value: WorkspaceSelection) {
  write(workspaceKey(userId), value);
}
export function readCaseSelection(userId: number, caseId: number): CaseSelection {
  const value = read(caseKey(userId, caseId));
  return {
    section: value.section === 'workflows' || value.section === 'batches' || value.section === 'remote' ? value.section : 'jobs',
    batchId: typeof value.batchId === 'string' && /^[0-9a-f]{32}$/.test(value.batchId) ? value.batchId : null,
    runId: identity(value.runId) ? value.runId : null,
    remoteRunId: identity(value.remoteRunId) ? value.remoteRunId : null,
    jobId: positiveId(value.jobId) ? value.jobId : null,
    attemptId: identity(value.attemptId) ? value.attemptId : null,
    caseRunId: identity(value.caseRunId) ? value.caseRunId : null,
    reading: reading(value.reading),
    previewTolerance: positivePreviewTolerance(value.previewTolerance) ? value.previewTolerance : undefined,
    cadTreeOpen: typeof value.cadTreeOpen === 'boolean' ? value.cadTreeOpen : undefined,
    cadAssembly: value.cadAssembly === true,
  };
}
export function saveCaseSelection(userId: number, caseId: number, patch: Partial<CaseSelection>) {
  write(caseKey(userId, caseId), { ...readCaseSelection(userId, caseId), ...patch });
}

export interface CadTreePreferences { hidden: string[]; collapsed: string[]; selected: string }
const cadTreeKey = (userId: number, caseId: number, mode: 'case' | 'assembly') => `vf-cad-tree-v1:${userId}:${caseId}:${mode}`;
const cadTreeId = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 800;
function cadTreeIds(value: unknown, maximum: number): string[] {
  return Array.isArray(value) ? value.slice(0, maximum).filter(cadTreeId) : [];
}
export function readCadTree(userId: number, caseId: number, mode: 'case' | 'assembly'): CadTreePreferences {
  const row = read(cadTreeKey(userId, caseId, mode));
  return { hidden: cadTreeIds(row.hidden, 64), collapsed: cadTreeIds(row.collapsed, 256), selected: cadTreeId(row.selected) ? row.selected : '' };
}
export function saveCadTree(userId: number, caseId: number, mode: 'case' | 'assembly', tree: CadTreePreferences) {
  write(cadTreeKey(userId, caseId, mode), tree);
}
const logKey = (userId: number, caseId: number, jobId: number, attemptId: string | null) =>
  `vf-job-log-cursor-v1:${userId}:${caseId}:${jobId}:${attemptId ?? 'legacy'}`;
export function readLogCursor(userId: number, caseId: number, jobId: number, attemptId: string | null) {
  const value = read(logKey(userId, caseId, jobId, attemptId)).offset;
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : 0;
}
export function saveLogCursor(userId: number, caseId: number, jobId: number, attemptId: string | null, offset: number) {
  write(logKey(userId, caseId, jobId, attemptId), { offset });
}
const publicationKey = (userId: number, caseId: number, channel: string) => `vf-publication-selection-v1:${userId}:${caseId}:${channel}`;
export function readPublicationSelection(userId: number, caseId: number, path: string): PublicationReading | null {
  const channel = publicationChannel(path);
  if (!channel) return null;
  try {
    const raw = sessionStorage.getItem(publicationKey(userId, caseId, channel));
    if (raw === null) return { path, channel, id: null };
    const value = JSON.parse(raw);
    return value && (value.id === null || identity(value.id))
      ? { path, channel, id: value.id } : { path, channel, id: null, invalid: true };
  } catch (failure) {
    return { path, channel, id: null, invalid: failure instanceof SyntaxError };
  }
}
export function savePublicationSelection(userId: number, caseId: number, value: PublicationReading) {
  if (publicationChannel(value.path) !== value.channel) return;
  write(publicationKey(userId, caseId, value.channel), { id: value.id });
  saveCaseSelection(userId, caseId, { reading: value });
}
const pdfViewKey = (userId: number, caseId: number, path: string, version: string) => `vf-pdf-view-v1:${userId}:${caseId}:${path}:${version}`;
export function readPdfView(userId: number, caseId: number, path: string, version: string) {
  const value = read(pdfViewKey(userId, caseId, path, version));
  return { page: Number.isSafeInteger(value.page) && Number(value.page) >= 1 ? Number(value.page) : 1,
    scale: typeof value.scale === 'number' && Number.isFinite(value.scale) && value.scale >= .4 && value.scale <= 3 ? value.scale : 1.2,
    rotation: value.rotation === 90 || value.rotation === 180 || value.rotation === 270 ? value.rotation : 0 };
}
export function savePdfView(userId: number, caseId: number, path: string, version: string, page: number, scale: number, rotation = 0) {
  write(pdfViewKey(userId, caseId, path, version), { page, scale, rotation });
}

export interface PublishedSourceHint { file: string; line: number }
const sourceKey = (userId: number, caseId: number, path: string, publicationId: string) =>
  `vf-pdf-source-v1:${userId}:${caseId}:${path}:${publicationId}`;
function validSourceHint(value: unknown): value is PublishedSourceHint {
  const hint = value as PublishedSourceHint | null;
  return !!hint && typeof hint.file === 'string' && hint.file.length > 0 && hint.file.length <= 2048
    && !/[\\:\u0000-\u001f]/.test(hint.file)
    && hint.file.split('/').every(part => !!part && part !== '.' && part !== '..')
    && Number.isSafeInteger(hint.line) && hint.line >= 1;
}
export function readPublishedSourceHint(userId: number, caseId: number, path: string, publicationId: string): PublishedSourceHint | null {
  let raw: string | null;
  try { raw = sessionStorage.getItem(sourceKey(userId, caseId, path, publicationId)); } catch { return null; }
  if (raw === null) return null;
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error('保存的源码位置无效，请重新双击 PDF 定位。'); }
  if (!validSourceHint(value)) throw new Error('保存的源码位置无效，请重新双击 PDF 定位。');
  return { file: value.file, line: value.line };
}
export function savePublishedSourceHint(userId: number, caseId: number, path: string, publicationId: string, hint: PublishedSourceHint | null) {
  if (hint && !validSourceHint(hint)) throw new Error('源码定位结果无效，请重新定位。');
  try {
    const key = sourceKey(userId, caseId, path, publicationId);
    if (hint) sessionStorage.setItem(key, JSON.stringify(hint)); else sessionStorage.removeItem(key);
  } catch { /* The current authorized source can still be read without persistence. */ }
}
export type DerivativeFamily = '' | 'draft' | 'pdf-number' | 'ppt';
const derivativeViewKey = (userId: number, caseId: number) => `vf-derivative-view-v1:${userId}:${caseId}`;
export function readDerivativeView(userId: number, caseId: number): { open: boolean; family: DerivativeFamily } {
  const value = read(derivativeViewKey(userId, caseId));
  return { open: value.open === true, family: typeof value.family === 'string' && ['draft', 'pdf-number', 'ppt'].includes(value.family) ? value.family as DerivativeFamily : '' };
}
export function saveDerivativeView(userId: number, caseId: number, open: boolean, family: DerivativeFamily) {
  write(derivativeViewKey(userId, caseId), { open, family });
}
const derivativeKey = (userId: number, caseId: number, family: DerivativeFamily) => `vf-derivative-selection-v1:${userId}:${caseId}:${family}`;
export function readDerivativeSelection(userId: number, caseId: number, family: DerivativeFamily): { id: string | null; invalid: boolean } {
  let raw: string | null;
  try { raw = sessionStorage.getItem(derivativeKey(userId, caseId, family)); } catch { return { id: null, invalid: false }; }
  if (raw === null) return { id: null, invalid: false };
  try {
    const value = JSON.parse(raw);
    if (value && (value.id === null || identity(value.id))) return { id: value.id, invalid: false };
  } catch { /* A malformed saved ID must not choose another version. */ }
  return { id: null, invalid: true };
}
export function saveDerivativeSelection(userId: number, caseId: number, family: DerivativeFamily, id: string | null) {
  write(derivativeKey(userId, caseId, family), { id });
}
