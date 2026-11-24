import { create } from "zustand";
import { api, ApiError } from "./api.ts";
import type { CaseInfo, TreeEntry, UserInfo, Workspace } from "./api";
import type { CaseTreeUpdate } from "./caseEventStream";
import type { PathFormat } from "./utils/pathRefs";
import type { FileVersion } from "./utils/uploadResume";
import { applyAppTheme, initialAppTheme } from "./utils/theme.ts";
import type { AppTheme } from "./utils/theme";
import { publicationChannel, readCaseSelection, readPublicationSelection, readWorkspaceSelection, saveCaseSelection, savePublicationSelection, saveWorkspaceSelection } from './utils/viewSelection.ts';
import type { PublicationReading } from './utils/viewSelection';
import type { ImageAttachment } from './panels/ChatImages';

let navigationGeneration = 0;
let fileOpenGeneration = 0;
let saveSequence = 0;
let treeReads: { generation: number; tail: Promise<unknown>; loaded: boolean; watermark: number | null } | undefined;

export interface FileOpenIntent {
  caseId: number;
  userId: number;
  isCurrent: () => boolean;
}

export function captureProjectCase(): FileOpenIntent | null {
  const { user, currentCase } = useStore.getState();
  if (!user || !currentCase) return null;
  const generation = navigationGeneration;
  return {
    caseId: currentCase.id, userId: user.id,
    isCurrent: () => {
      const state = useStore.getState();
      return generation === navigationGeneration && state.user?.id === user.id && state.currentCase?.id === currentCase.id;
    },
  };
}

export function beginFileOpen(): FileOpenIntent | null {
  const project = captureProjectCase();
  if (!project) return null;
  const generation = ++fileOpenGeneration;
  return { ...project, isCurrent: () => generation === fileOpenGeneration && project.isCurrent() };
}

export type FilePreviewRequest = { intent: FileOpenIntent } & (
  { kind: "step"; path: string } | { kind: "viz"; runDir?: string; filePath?: string }
);

export interface OpenFile {
  path: string;
  kind: "text" | "binary";
  content?: string;
  size: number;
  download_url?: string;
  language?: "openfoam-dict";
  dirty?: boolean;
  openedMtime?: string;   // 打开时的文件修改时间（外部修改冲突检测用）
  content_sha256?: string | null;
  dict_revision?: number;
  editorId?: string;
  saving?: boolean;
  saveError?: string;
  conflict?: boolean;
  serverVersion?: OpenFile | null;
}

interface AutoPreviewError {
  code: string;
  message: string;
}

interface SaveFileResult {
  ok: boolean;
  content_sha256: string;
  dict_revision?: number;
  auto_preview_job?: number | null;
  auto_preview_error?: AutoPreviewError;
}

interface AutoPreviewHandoff {
  sequence: number;
  jobId?: number;
  error?: AutoPreviewError;
}

const isCaseDictPath = (path: string) => path.replace(/\\/g, "/").replace(/^\.\//, "") === "0-caseDict/caseDict";

async function readEditorFile(caseId: number, path: string): Promise<OpenFile> {
  if (isCaseDictPath(path)) {
    const snapshot = await api.get(`/cases/${caseId}/caseDict`);
    return {path, kind:"text", content:snapshot.content, content_sha256:snapshot.content_sha256,
      dict_revision:snapshot.dict_revision, size:new TextEncoder().encode(snapshot.content).length};
  }
  return api.get<OpenFile>(`/cases/${caseId}/file?path=${encodeURIComponent(path)}`);
}

export interface AgentDraft {
  input: string;
  images: ImageAttachment[];
}

const emptyAgentDraft: AgentDraft = { input: "", images: [] };

export function currentAgentDraft(state: State): AgentDraft {
  return (state.currentCase && state.agentDrafts[state.currentCase.id]) || emptyAgentDraft;
}

export function withoutSentAgentDraft(draft: AgentDraft, sent: AgentDraft): AgentDraft {
  return {
    input: draft.input === sent.input ? "" : draft.input,
    images: draft.images.filter(image => !sent.images.includes(image)),
  };
}

export function clearSubmittedAgentDraft(project: FileOpenIntent, sent: AgentDraft): void {
  if (project.isCurrent()) {
    useStore.getState().updateAgentDraft(draft => withoutSentAgentDraft(draft, sent));
    return;
  }
  // Navigation can leave a native submission in flight. Its original draft can be
  // removed after acknowledgement only while the exact snapshot is still retained.
  useStore.setState(state => {
    if (state.user?.id !== project.userId || state.agentDrafts[project.caseId] !== sent) return state;
    const agentDrafts = { ...state.agentDrafts };
    delete agentDrafts[project.caseId];
    return { agentDrafts };
  });
}

interface State {
  user: UserInfo | null;
  workspaces: Workspace[];
  currentWs: Workspace | null;
  cases: CaseInfo[];
  currentCase: (CaseInfo & { workspace_id?: number }) | null;
  fileTree: TreeEntry[];
  openFiles: Record<string, OpenFile>;
  activeFile: string | null;
  filePreviewRequest: FilePreviewRequest | null;
  activePublication: (PublicationReading & { caseId: number }) | null;
  documentAnchor: { caseId: number; path: string; hash: string; requestId: string } | null;
  autoPreviewHandoffs: Record<number, AutoPreviewHandoff>;
  workflowSelection: { caseId: number; runId: string } | null;
  jobPanelRequest: { caseId: number; runId?: string; jobId?: number } | null;
  pythonConsoleRequest: { caseId: number; userId: number } | null;
  viewRecoveryNotice: string;
  projectLoadError: {target: 'workspace' | 'case'; message: string} | null;
  casesLoading: boolean;
  agentDrafts: Record<number, AgentDraft>;
  pathFormat: PathFormat;
  theme: AppTheme;

  setUser: (u: UserInfo | null) => void;
  setTheme: (theme: AppTheme) => void;
  updateAgentDraft: (edit: (draft: AgentDraft) => AgentDraft) => void;
  setWorkflowSelection: (value: { caseId: number; runId: string } | null) => void;
  requestJobPanel: (value: { caseId: number; runId?: string; jobId?: number }) => void;
  requestPythonConsole: (value: { caseId: number; userId: number } | null) => void;
  setPathFormat: (format: PathFormat) => void;
  loadWorkspaces: () => Promise<void>;
  selectWorkspace: (w: Workspace, restoreCaseId?: number | null) => Promise<void>;
  retryProjectLoad: () => Promise<void>;
  createWorkspace: (name: string) => Promise<void>;
  selectCase: (c: CaseInfo) => Promise<void>;
  createCase: (name: string) => Promise<void>;
  importCase: (name: string, file: File) => Promise<void>;
  refreshCase: (id: number) => Promise<void>;

  loadTree: () => Promise<void>;
  syncTree: (update: CaseTreeUpdate) => Promise<number | null>;
  openFile: (path: string, hash?: string, intent?: FileOpenIntent | null) => Promise<boolean>;
  setActiveFile: (p: string | null, hash?: string, intent?: FileOpenIntent) => void;
  clearDocumentAnchor: (requestId: string) => void;
  selectPublication: (userId: number, caseId: number, path: string, id: string | null) => void;
  rememberPublication: (userId: number, caseId: number, path: string, expected: string | null, id: string) => void;
  closeFile: (p: string) => void;
  editContent: (path: string, content: string) => void;
  saveFile: (path: string) => Promise<SaveFileResult | null>;
  saveAll: () => Promise<void>;
  clearAutoPreviewError: (caseId: number, error: AutoPreviewError) => void;
  inspectServerVersion: (path: string) => Promise<void>;
  resolveFileConflict: (path: string, keepDraft: boolean) => void;
  closeAll: () => void;
}

export const useStore = create<State>((set, get) => ({
  user: null,
  workspaces: [],
  currentWs: null,
  cases: [],
  currentCase: null,
  fileTree: [],
  openFiles: {},
  activeFile: null,
  filePreviewRequest: null,
  activePublication: null,
  documentAnchor: null,
  autoPreviewHandoffs: {},
  workflowSelection: null,
  jobPanelRequest: null,
  pythonConsoleRequest: null,
  viewRecoveryNotice: '',
  projectLoadError: null,
  casesLoading: false,
  agentDrafts: {},
  pathFormat: localStorage.getItem("vf_path_format") === "windows" ? "windows" : "linux",
  theme: initialAppTheme(),

  setUser: (user) => {
    if (user?.id === get().user?.id) { set({ user }); return; }
    ++navigationGeneration;
    set({ user, workspaces: [], currentWs: null, cases: [], currentCase: null, fileTree: [],
      openFiles: {}, activeFile: null, filePreviewRequest: null, activePublication: null, documentAnchor: null, autoPreviewHandoffs: {}, workflowSelection: null,
      jobPanelRequest: null, pythonConsoleRequest: null, viewRecoveryNotice: '', projectLoadError: null, casesLoading: false, agentDrafts: {} });
  },
  setTheme: (theme) => {
    localStorage.setItem("vf_theme", theme);
    applyAppTheme(theme);
    set({ theme });
  },
  updateAgentDraft: (edit) => set(state => {
    if (!state.user || !state.currentCase) return state;
    const caseId = state.currentCase.id;
    const current = currentAgentDraft(state);
    const next = edit(current);
    if (next === current) return state;
    const agentDrafts = { ...state.agentDrafts };
    if (next.input || next.images.length) agentDrafts[caseId] = next;
    else delete agentDrafts[caseId];
    return { agentDrafts };
  }),
  setWorkflowSelection: (value) => {
    if (value && value.caseId !== get().currentCase?.id) return;
    const userId = get().user?.id, caseId = get().currentCase?.id;
    if (userId && caseId) saveCaseSelection(userId, caseId, { caseRunId: value?.runId ?? null });
    set({ workflowSelection: value });
  },
  requestJobPanel: (value) => {
    if (value.caseId !== get().currentCase?.id) return;
    set({ jobPanelRequest: value });
  },
  requestPythonConsole: value => set({ pythonConsoleRequest: value }),
  setPathFormat: (pathFormat) => {
    localStorage.setItem("vf_path_format", pathFormat);
    set({ pathFormat });
  },

  loadWorkspaces: async () => {
    const userId = get().user?.id, generation = navigationGeneration;
    if (!userId) return;
    const list = await api.get<Workspace[]>("/workspaces");
    if (get().user?.id !== userId || navigationGeneration !== generation) return;
    set({ workspaces: list });
    if (list.length && !get().currentWs) {
      const saved = readWorkspaceSelection(userId);
      const workspace = list.find(value => value.id === saved?.workspaceId) ?? list[0];
      await get().selectWorkspace(workspace, saved?.workspaceId === workspace.id ? saved.caseId : null);
      if (get().user?.id !== userId || navigationGeneration !== generation + 1 || get().currentWs?.id !== workspace.id) return;
      if (saved && saved.workspaceId !== workspace.id && !get().projectLoadError) {
        set({ viewRecoveryNotice: '原工作区已不存在或无权访问，请重新选择算例。' });
      }
    }
  },

  selectWorkspace: async (w, restoreCaseId = null) => {
    const userId = get().user?.id, generation = ++navigationGeneration;
    if (!userId) return;
    saveWorkspaceSelection(userId, { workspaceId: w.id, caseId: restoreCaseId });
    set({ currentWs: w, currentCase: null, cases: [], casesLoading: true, fileTree: [], openFiles: {}, activeFile: null, filePreviewRequest: null,
      activePublication: null, documentAnchor: null, workflowSelection: null,
      jobPanelRequest: null, pythonConsoleRequest: null, viewRecoveryNotice: '', projectLoadError: null });
    let cases: CaseInfo[];
    try {
      cases = await api.get<CaseInfo[]>(`/workspaces/${w.id}/cases`);
    } catch {
      if (generation === navigationGeneration) set({casesLoading: false,
        projectLoadError: {target: 'workspace', message: '算例列表加载失败。'}});
      return;
    }
    if (generation !== navigationGeneration) return;
    set({cases, casesLoading: false});
    if (restoreCaseId === null) return;
    const saved = cases.find(c => c.id === restoreCaseId);
    if (saved) await get().selectCase(saved);
    else {
      saveWorkspaceSelection(userId, {workspaceId: w.id, caseId: null});
      set({viewRecoveryNotice: '原算例已不存在、已移动或无权访问，请重新选择。'});
    }
  },

  retryProjectLoad: async () => {
    const {currentWs, projectLoadError, user} = get();
    if (!currentWs || !projectLoadError || !user) return;
    if (projectLoadError.target === 'workspace') {
      const saved = readWorkspaceSelection(user.id);
      await get().selectWorkspace(currentWs, saved?.workspaceId === currentWs.id ? saved.caseId : null);
      return;
    }
    const generation = navigationGeneration;
    set({projectLoadError: null});
    try {
      const caseId = get().currentCase?.id;
      const detail = currentWs.shared && caseId ? get().refreshCase(caseId) : Promise.resolve();
      await Promise.all([get().loadTree(), detail]);
    } catch {
      if (generation === navigationGeneration) set({projectLoadError});
    }
  },

  createWorkspace: async (name) => {
    const userId = get().user?.id, generation = navigationGeneration;
    if (!userId) return;
    let created = await api.post<Workspace>("/workspaces", { name });
    if (get().user?.id !== userId) return;
    if (!created.created_at) {
      const list = await api.get<Workspace[]>("/workspaces");
      created = list.find(w => w.id === created.id) ?? created;
    }
    if (get().user?.id !== userId) return;
    if (!get().workspaces.some(w => w.id === created.id)) set({workspaces: [...get().workspaces, created]});
    if (generation === navigationGeneration) void get().selectWorkspace(created);
  },

  selectCase: async (c) => {
    const userId = get().user?.id, workspaceId = get().currentWs?.id;
    if (!userId || !workspaceId || !get().cases.some(value => value.id === c.id)) return;
    if (get().currentCase?.id === c.id) return;
    const generation = ++navigationGeneration;
    const saved = readCaseSelection(userId, c.id);
    saveWorkspaceSelection(userId, { workspaceId, caseId: c.id });
    set({ currentCase: c, fileTree: [], openFiles: {}, activeFile: saved.reading?.path ?? null, filePreviewRequest: null,
      activePublication: saved.reading ? { ...saved.reading, caseId: c.id } : null, documentAnchor: null,
      workflowSelection: saved.caseRunId ? { caseId: c.id, runId: saved.caseRunId } : null,
      jobPanelRequest: null, pythonConsoleRequest: null, viewRecoveryNotice: '', projectLoadError: null });
    try {
      const detail = get().currentWs?.shared ? get().refreshCase(c.id) : Promise.resolve();
      await Promise.all([get().loadTree(), detail]);
    } catch {
      if (navigationGeneration === generation) set({projectLoadError: {target: 'case', message: '文件树加载失败。'}});
    }
  },

  createCase: name => createAndSelectCase(id => api.post<CaseInfo>(`/workspaces/${id}/cases`, {name})),

  importCase: async (name, file) => {
    const form = new FormData();
    form.append("name", name);
    form.append("file", file);
    await createAndSelectCase(id => api.upload<CaseInfo>(`/workspaces/${id}/cases:import`, form));
  },

  refreshCase: async (id) => {
    const userId = get().user?.id, generation = navigationGeneration;
    const c = await api.get<CaseInfo & { workspace_id: number }>(`/cases/${id}`);
    if (get().user?.id !== userId || generation !== navigationGeneration) return;
    set({
      currentCase: get().currentCase?.id === id ? { ...get().currentCase!, ...c } : get().currentCase,
      cases: get().cases.map((x) => (x.id === id ? { ...x, ...c } : x)),
    });
  },

  loadTree: async () => {
    await get().syncTree({full: true, paths: [], initial: false});
  },

  syncTree: ({full, paths, initial, through}) => {
    const cc = get().currentCase;
    const userId = get().user?.id, generation = navigationGeneration;
    if (!cc || (!full && !paths.length)) return Promise.resolve(null);
    if (treeReads?.generation !== generation) {
      treeReads = { generation, tail: Promise.resolve(), loaded: false, watermark: null };
    }
    const reads = treeReads;
    const isCurrent = () => get().user?.id === userId && navigationGeneration === generation && get().currentCase?.id === cc.id;
    const operation = reads.tail.catch(() => {}).then(async () => {
      if (!isCurrent()) return null;
      if (initial && reads.watermark !== null && through !== undefined && reads.watermark >= through) return reads.watermark;
      if (!full && reads.loaded) {
        let result: { entries: TreeEntry[] | null } | undefined;
        try {
          result = await api.post(`/cases/${cc.id}/tree:patch`, {paths});
        } catch (error) {
          if (!(error instanceof ApiError) || ![404,405,422].includes(error.status)) throw error;
        }
        if (!isCurrent()) return null;
        const requested = new Set(paths);
        if (Array.isArray(result?.entries) && result.entries.every(entry => requested.has(entry?.path))) {
          const entries = new Map(get().fileTree.map(entry => [entry.path, entry]));
          const replacements = new Map(result.entries.map(entry => [entry.path, entry]));
          let changed = false;
          for (const path of paths) {
            const next = replacements.get(path), previous = entries.get(path);
            if (!next) changed = entries.delete(path) || changed;
            else if (!previous || previous.size !== next.size || previous.modified !== next.modified || previous.kind !== next.kind) {
              entries.set(path, next);
              changed = true;
            }
          }
          if (changed) set({fileTree: [...entries.values()]});
          return null;
        }
      }
      const r = await api.get<{entries: TreeEntry[]; watermark?: number}>(`/cases/${cc.id}/tree`);
      if (!isCurrent()) return null;
      if (!Array.isArray(r.entries)) throw new Error('Invalid file tree');
      reads.loaded = true;
      reads.watermark = Number.isSafeInteger(r.watermark) ? r.watermark! : null;
      set({fileTree: r.entries, projectLoadError: null});
      return reads.watermark;
    });
    reads.tail = operation;
    return operation;
  },

  openFile: async (path, hash, intent = beginFileOpen()) => {
    if (!intent?.isCurrent()) return false;
    const publication = readPublicationSelection(intent.userId, intent.caseId, path);
    // Frozen publications can outlive their mutable file aliases.
    if (!get().openFiles[path] && !publication?.id && !publication?.invalid) {
      try {
        const f = await readEditorFile(intent.caseId, path);
        if (!intent.isCurrent()) return false;
        const ent = get().fileTree.find((entry) => entry.path === path);
        if (!get().openFiles[path]) set({ openFiles: { ...get().openFiles,
          [path]: { ...f, path, editorId:crypto.randomUUID(), openedMtime: ent?.modified } } });
      } catch (error) {
        if (intent.isCurrent()) throw error;
        return false;
      }
    }
    get().setActiveFile(path, hash, intent);
    return true;
  },

  setActiveFile: (path, hash, intent) => {
    if (intent && !intent.isCurrent()) return;
    if (!intent) ++fileOpenGeneration;
    const userId = get().user?.id, caseId = get().currentCase?.id;
    const publication = userId && caseId && path ? readPublicationSelection(userId, caseId, path) : null;
    if (userId && caseId) saveCaseSelection(userId, caseId, { reading: publication });
    set({ activeFile: path, activePublication: publication && caseId ? { ...publication, caseId } : null,
      documentAnchor: caseId && path && hash ? { caseId, path, hash, requestId: crypto.randomUUID() } : null });
  },
  clearDocumentAnchor: (requestId) => {
    if (get().documentAnchor?.requestId === requestId) set({ documentAnchor: null });
  },
  selectPublication: (userId, caseId, path, id) => {
    const channel = publicationChannel(path);
    if (!channel || get().user?.id !== userId || get().currentCase?.id !== caseId || get().activeFile !== path) return;
    const value = { path, channel, id };
    savePublicationSelection(userId, caseId, value);
    set({ activePublication: { ...value, caseId }, documentAnchor: null });
  },
  rememberPublication: (userId, caseId, path, expected, id) => {
    const active = get().activePublication;
    if (get().user?.id !== userId || get().activeFile !== path || active?.caseId !== caseId || active.path !== path || active.id !== expected || active.invalid) return;
    // Persist the resolved latest ID without restarting an already opened PDF/document.
    savePublicationSelection(userId, caseId, { path, channel: active.channel, id });
  },

  closeFile: (p) => {
    const files = { ...get().openFiles };
    delete files[p];
    const active = get().activeFile === p ? Object.keys(files)[0] ?? null : get().activeFile;
    set({ openFiles: files });
    get().setActiveFile(active);
  },

  editContent: (path, content) => {
    const f = get().openFiles[path];
    if (!f) return;
    set({ openFiles: { ...get().openFiles, [path]: { ...f, content, dirty: true } } });
  },

  clearAutoPreviewError: (caseId, error) => {
    set(state => {
      const handoff = state.autoPreviewHandoffs[caseId];
      if (handoff?.error !== error) return state;
      return {autoPreviewHandoffs: {...state.autoPreviewHandoffs, [caseId]: {sequence: handoff.sequence}}};
    });
  },

  saveFile: async (path) => {
    const cc = get().currentCase;
    const user = get().user;
    const f = get().openFiles[path];
    if (!cc || !f || f.kind !== "text" || f.saving || !f.dirty) return null;
    const sequence = ++saveSequence;
    const currentEditor = () => get().user?.id === user?.id && get().currentCase?.id === cc.id && Boolean(get().openFiles[path]) &&
      get().openFiles[path]?.editorId === f.editorId;
    const update = (fields: Partial<OpenFile>) => {
      if (currentEditor()) set({openFiles:{...get().openFiles, [path]:{...get().openFiles[path], ...fields}}});
    };
    update({saving:true, saveError:undefined});
    try {
      const isDict = isCaseDictPath(path);
      if (!f.content_sha256 && (f.content_sha256 !== null || isDict)) {
        throw new Error("无法确认文件版本，草稿已保留。请查看服务器版本后再保存。");
      }
      const content = f.content ?? "";
      const body = {content, expected_sha256:f.content_sha256,
        ...(isDict ? {expected_revision:f.dict_revision} : {path})};
      const result = await api.put<SaveFileResult>(`/cases/${cc.id}/${isDict ? "caseDict" : "file"}`, body);
      const {auto_preview_job: jobId, auto_preview_error: error} = result;
      if ((jobId || error) && user) {
        set(state => state.user?.id === user.id && sequence > (state.autoPreviewHandoffs[cc.id]?.sequence ?? 0)
          ? {autoPreviewHandoffs: {...state.autoPreviewHandoffs, [cc.id]: {sequence, jobId: jobId ?? undefined, error}}}
          : state);
      }
      // Only the submitted text was saved. Keep edits typed while awaiting the response.
      update({dirty:get().openFiles[path]?.content !== content,
        content_sha256:result.content_sha256, dict_revision:result.dict_revision ?? f.dict_revision,
        conflict:false, saveError:undefined, serverVersion:undefined});
      if (currentEditor()) {
        // A failed refresh does not turn a committed write into an apparent failed save.
        await Promise.allSettled([get().loadTree(), ...(isDict ? [get().refreshCase(cc.id)] : [])]);
      }
      return result;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409 &&
        (error.code === "revision_conflict" || error.code === "file_conflict");
      update({saveError:conflict ? "服务器文件已变化，本地草稿已保留。请查看服务器版本后再保存。"
        : error instanceof Error ? error.message : String(error), conflict, serverVersion:undefined});
      throw error;
    } finally {
      update({saving:false});
    }
  },

  inspectServerVersion: async (path) => {
    const cc = get().currentCase;
    const editor = get().openFiles[path];
    if (!cc || !editor || editor.saving) return;
    const generation = navigationGeneration;
    let snapshot: OpenFile | null;
    try {
      snapshot = await readEditorFile(cc.id, path);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404 || isCaseDictPath(path)) throw error;
      const version = await api.get<FileVersion>(`/cases/${cc.id}/file:version?path=${encodeURIComponent(path)}`);
      if (version.exists || version.content_sha256 !== null || version.protected) throw error;
      snapshot = null;
    }
    const current = get().openFiles[path];
    if (navigationGeneration !== generation || get().currentCase?.id !== cc.id || !current ||
        current.editorId !== editor.editorId || current.content_sha256 !== editor.content_sha256 ||
        current.saveError !== editor.saveError || current.saving) return;
    set({openFiles:{...get().openFiles, [path]:{...current, serverVersion:snapshot}}});
  },

  resolveFileConflict: (path, keepDraft) => {
    const editor = get().openFiles[path];
    if (!editor || editor.saving || editor.serverVersion === undefined) return;
    const server = editor.serverVersion;
    if (server === null && !keepDraft) {
      get().closeFile(path);
      return;
    }
    if (server && !server.content_sha256) return;
    set({openFiles:{...get().openFiles, [path]:{...editor,
      content:keepDraft ? editor.content : server?.content, content_sha256:server === null ? null : server.content_sha256,
      dict_revision:server?.dict_revision, dirty:keepDraft && (server === null || editor.content !== server.content),
      conflict:false, saveError:undefined, serverVersion:undefined}}});
  },

  saveAll: async () => {
    const selection = captureProjectCase();
    if (!selection) return;
    const dirty = Object.entries(get().openFiles)
      .filter(([, file]) => file.kind === "text" && file.dirty)
      .map(([path, file]) => ({path, editorId: file.editorId}));
    const failures: unknown[] = [];
    // File writes share one storage owner. Submit each file after the previous
    // write has settled, and stop if the user has switched to another case.
    for (const {path, editorId} of dirty) {
      if (!selection.isCurrent()) return;
      if (get().openFiles[path]?.editorId !== editorId) continue;
      try { await get().saveFile(path); }
      catch (error) { failures.push(error); }
    }
    if (!selection.isCurrent()) return;
    if (failures.length) throw failures[0];
    if (dirty.some(({path}) => get().openFiles[path]?.dirty))
      throw new Error("仍有编辑尚未保存，请等待正在进行的保存完成后再次保存。");
  },

  closeAll: () => {
    ++fileOpenGeneration;
    const userId = get().user?.id, caseId = get().currentCase?.id;
    if (userId && caseId) saveCaseSelection(userId, caseId, { reading: null });
    set({ openFiles: {}, activeFile: null, activePublication: null, documentAnchor: null });
  },
}));

async function createAndSelectCase(submit: (workspaceId: number) => Promise<CaseInfo>) {
  const {user, currentWs} = useStore.getState();
  if (!user || !currentWs) return;
  const generation = navigationGeneration;
  const inWorkspace = () => useStore.getState().currentWs?.id === currentWs.id && useStore.getState().user?.id === user.id;
  let created = await submit(currentWs.id);
  if (!inWorkspace()) return;
  if (!created.updated_at) {
    // Older APIs return only the identity. Resolve the full record in its original workspace.
    const list = await api.get<CaseInfo[]>(`/workspaces/${currentWs.id}/cases`);
    const found = list.find(c => c.id === created.id);
    if (!found || !inWorkspace()) return;
    created = found;
  }
  const state = useStore.getState();
  if (!state.cases.some(c => c.id === created.id)) useStore.setState({cases: [...state.cases, created]});
  if (navigationGeneration === generation && !state.casesLoading) void state.selectCase(created);
}
