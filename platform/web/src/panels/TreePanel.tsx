import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { watchCaseEvents } from "../caseEventStream";
import type { CaseInfo, TreeEntry, Workspace } from "../api";
import { isCaseReadOnly } from "../api";
import { openProjectFile } from "../shell/openProjectFile";
import { beginFileOpen, captureProjectCase, useStore } from "../store";
import CaseFileActions from "./CaseFileActions";
import ManualFileRecovery from './ManualFileRecovery';
import ProjectAccess from './ProjectAccess';

/** 目录树节点（由平铺 path 列表构建） */
interface Node {
  name: string;
  path: string;
  isDir: boolean;
  children: Node[];
}

function buildTree(entries: TreeEntry[]): Node[] {
  const root: Node = { name: "", path: "", isDir: true, children: [] };
  const byPath = new Map<string, Node>();
  for (const e of entries) {
    const parts = e.path.split("/");
    let cur = root;
    parts.forEach((p, i) => {
      const isLast = i === parts.length - 1;
      const path = cur.path ? `${cur.path}/${p}` : p;
      let child = byPath.get(path);
      if (!child) {
        child = { name: p, path, isDir: !isLast || e.kind === "directory", children: [] };
        byPath.set(path, child);
        cur.children.push(child);
      }
      if (isLast) child.isDir = e.kind === "directory";
      cur = child;
    });
  }
  const sort = (ns: Node[]) => {
    ns.sort((a, b) => Number(b.isDir) - Number(a.isDir) || a.name.localeCompare(b.name));
    ns.forEach((n) => sort(n.children));
  };
  sort(root.children);
  return root.children;
}

function TreeNodes({ nodes, depth, onOpen }: { nodes: Node[]; depth: number; onOpen: (path: string) => void }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const activeFile = useStore((s) => s.activeFile);
  return (
    <>
      {nodes.map((n) => {
        const isOpen = open[n.path] ?? depth === 0;
        return (
          <div key={n.path}>
            <button type="button"
              data-path={n.path}
              data-dir={n.isDir ? "true" : "false"}
              title={n.path}
              aria-expanded={n.isDir ? isOpen : undefined}
              style={{
                display: "block", width: "100%", border: 0, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis",
                paddingLeft: depth * 14 + 6,
                cursor: "pointer",
                color: n.isDir ? "#9ab" : activeFile === n.path ? "#7db4ff" : "#cfd6e4",
                background: activeFile === n.path ? "#1c2740" : "transparent",
                whiteSpace: "nowrap",
                lineHeight: "22px",
                fontSize: 13,
              }}
              onClick={() => {
                if (n.isDir) { setOpen({ ...open, [n.path]: !isOpen }); return; }
                onOpen(n.path);
              }}
            >
              {n.isDir ? (isOpen ? "▾ " : "▸ ") : "  "}
              {n.isDir ? "📁" : "📄"} {n.name}
            </button>
            {n.isDir && isOpen && <TreeNodes nodes={n.children} depth={depth + 1} onOpen={onOpen} />}
          </div>
        );
      })}
    </>
  );
}

const STATE_LABEL: Record<string, string> = {
  A: "几何", B: "网格", C: "求解", D: "后处理", E: "报告", V: "开发",
};

function CreationForm({kind, create, importZip, disabled = false}: {
  kind: '工作区' | '算例';
  create: (name: string) => Promise<void>;
  importZip?: (name: string, file: File) => Promise<void>;
  disabled?: boolean;
}) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  const submit = async (file?: File) => {
    const value = name.trim() || file?.name.replace(/\.zip$/i, '');
    if (!value || disabled || pending.current) return;
    pending.current = true;
    setBusy(file ? '导入' : '创建');
    setError('');
    try {
      if (file && importZip) await importZip(value, file);
      else await create(value);
      setName('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      pending.current = false;
      setBusy('');
    }
  };
  return <>
    <form className="vf-project-create" aria-label={`创建${kind}`} onSubmit={e => { e.preventDefault(); void submit(); }}>
      <input aria-label={`新${kind}名`} placeholder={importZip ? '新算例/导入名' : '新工作区名'}
        value={name} disabled={disabled || !!busy} onChange={e => {setName(e.target.value); setError('');}} />
      <button type="submit" aria-label={`创建${kind}`} disabled={disabled || !!busy || !name.trim()}>
        {busy === '创建' ? '创建中…' : '创建'}
      </button>
      {importZip && <>
        <button type="button" title="导入 StandardCase ZIP" disabled={disabled || !!busy}
          onClick={() => fileInput.current?.click()}>{busy === '导入' ? '导入中…' : '导入'}</button>
        <input ref={fileInput} data-testid="case-import-file" type="file" accept=".zip,application/zip" hidden
          disabled={disabled || !!busy} onChange={e => {
            const file = e.currentTarget.files?.[0];
            e.currentTarget.value = '';
            if (file) void submit(file);
          }} />
      </>}
    </form>
    {error && <p role="alert" className="vf-project-error">{error}</p>}
  </>;
}

export default function TreePanel() {
  const { workspaces, currentWs, cases, currentCase, fileTree, user, viewRecoveryNotice,
    projectLoadError, casesLoading, retryProjectLoad } = useStore();
  const caseReadOnly = isCaseReadOnly(currentCase, currentWs);
  const { selectWorkspace, createWorkspace, selectCase, createCase, importCase, loadWorkspaces } = useStore();
  const [manualSource,setManualSource]=useState<{caseId:number;userId:number;source:string;token:string}|null>(null);
  useEffect(()=>setManualSource(null),[currentCase?.id,user?.id]);
  const [adminBusy, setAdminBusy] = useState(false);
  const [sharingOpen, setSharingOpen] = useState(false);
  const [adminError, setAdminError] = useState("");
  const [moveTarget, setMoveTarget] = useState<string | null>(null);
  const ownsWorkspace = Boolean(currentWs && !currentWs.shared);
  const moveTargets = workspaces.filter(item => !item.shared && item.id !== currentWs?.id);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<{ path: string; name?: string; snippet?: string; via: string;
    case_id?: number; case_name?: string }[] | null>(null);
  const [searchScope, setSearchScope] = useState<"case" | "workspace">("case");
  const [trashOpen, setTrashOpen] = useState(false);
  const [trash, setTrash] = useState<{ cases: { id: number; name: string; deleted_at: string }[];
    workspaces: { id: number; name: string; deleted_at: string }[] }>({ cases: [], workspaces: [] });
  const [idxReady, setIdxReady] = useState<boolean | null>(null);
  const [indexError, setIndexError] = useState('');
  const [syncError, setSyncError] = useState('');
  const [searchError, setSearchError] = useState('');
  const [fileOpen, setFileOpen] = useState<{ path: string; message: string; failed: boolean; original: boolean } | null>(null);
  const searchGeneration = useRef(0);
  const tree = useMemo(() => buildTree(fileTree), [fileTree]);

  useEffect(() => setFileOpen(null), [currentCase?.id, user?.id]);
  useEffect(() => setMoveTarget(null), [currentWs?.id, currentCase?.id, user?.id]);

  const openFromTree = async (path: string, original = false) => {
    const intent = beginFileOpen();
    if (!intent) return false;
    const name = path.split('/').at(-1) ?? path;
    const attempt = { path, message: `正在打开 ${name}…`, failed: false, original };
    setFileOpen(attempt);
    try {
      return await openProjectFile(path, { intent, original });
    } catch (error) {
      if (intent.isCurrent()) {
        const detail = error instanceof Error ? error.message : String(error);
        setFileOpen({ ...attempt, failed: true, message: `无法打开 ${name}。${detail}` });
      }
      return false;
    } finally {
      setFileOpen(current => current === attempt ? null : current);
    }
  };

  const runAdmin = async (action: () => Promise<void>) => {
    setAdminBusy(true);
    setAdminError("");
    try { await action(); }
    catch (error) { setAdminError(error instanceof Error ? error.message : String(error)); }
    finally { setAdminBusy(false); }
  };

  const hasDrafts = () => Object.values(useStore.getState().openFiles).some(file => file.dirty);
  const confirmLeavingDrafts = () => !hasDrafts() || confirm("有未保存的编辑。确定丢弃草稿并继续？");

  const renameCurrentWorkspace = () => runAdmin(async () => {
    if (!currentWs || !user) return;
    const name = prompt("工作区新名称", currentWs.name)?.trim();
    if (!name) return;
    const updated = await api.patch<Pick<Workspace, 'id' | 'name'>>(`/workspaces/${currentWs.id}`, { name });
    useStore.setState(state => state.user?.id !== user.id ? state : ({
      workspaces: state.workspaces.map(workspace => workspace.id === updated.id ? { ...workspace, ...updated } : workspace),
      currentWs: state.currentWs?.id === updated.id ? { ...state.currentWs, ...updated } : state.currentWs,
    }));
  });

  const copyCurrentWorkspace = () => runAdmin(async () => {
    if (!currentWs) return;
    const name = prompt("复制工作区为（包含全部算例）", `${currentWs.name}-copy`)?.trim();
    if (!name || !confirmLeavingDrafts()) return;
    const created = await api.post<Workspace>(`/workspaces/${currentWs.id}:copy`, { name, include_cases: true });
    await loadWorkspaces();
    const copied = useStore.getState().workspaces.find((w) => w.id === created.id);
    if (copied) await selectWorkspace(copied);
  });

  const renameCurrentCase = () => runAdmin(async () => {
    if (!currentCase) return;
    const name = prompt("算例新名称", currentCase.name)?.trim();
    if (!name) return;
    await api.patch(`/cases/${currentCase.id}`, { name });
    await useStore.getState().refreshCase(currentCase.id);
  });

  const moveCurrentCase = () => runAdmin(async () => {
    const selection = captureProjectCase();
    const target = moveTargets.find(workspace => String(workspace.id) === moveTarget);
    if (!selection || !target || !ownsWorkspace || !confirmLeavingDrafts()) return;
    const moved = await api.post<CaseInfo>(`/cases/${selection.caseId}:move`, { workspace_id: target.id });
    if (selection.isCurrent()) await selectWorkspace(target, moved.id);
  });

  const copyCurrentCase = () => runAdmin(async () => {
    if (!currentCase || !currentWs) return;
    const name = prompt("复制算例为", `${currentCase.name}-copy`)?.trim();
    if (!name || !confirmLeavingDrafts()) return;
    const copied = await api.post<CaseInfo>(`/cases/${currentCase.id}:copy`,
      { name, workspace_id: currentWs.id });
    await selectWorkspace(currentWs);
    await selectCase(copied);
  });

  const loadTrash = async () => setTrash(await api.get("/trash"));
  useEffect(() => { if (trashOpen) void loadTrash(); }, [trashOpen]);

  const deleteCurrentCase = () => runAdmin(async () => {
    if (!currentCase || !currentWs) return;
    if (!confirm(`将算例「${currentCase.name}」移入回收站？${hasDrafts() ? '未保存的编辑将丢弃。' : ''}`)) return;
    await api.del(`/cases/${currentCase.id}`);
    await selectWorkspace(currentWs);
    if (trashOpen) await loadTrash();
  });

  const deleteCurrentWorkspace = () => runAdmin(async () => {
    if (!currentWs) return;
    if (!confirm(`将工作区「${currentWs.name}」及其中算例移入回收站？${hasDrafts() ? '未保存的编辑将丢弃。' : ''}`)) return;
    await api.del(`/workspaces/${currentWs.id}`);
    const remaining = await api.get<Workspace[]>("/workspaces");
    useStore.setState({ workspaces: remaining });
    if (remaining.length) await selectWorkspace(remaining[0]);
    else useStore.setState({ currentWs: null, currentCase: null, cases: [], fileTree: [],
      openFiles: {}, activeFile: null });
    if (trashOpen) await loadTrash();
  });

  const restoreCase = (id: number) => runAdmin(async () => {
    await api.post(`/cases/${id}:restore`, {});
    if (currentWs) await selectWorkspace(currentWs);
    await loadTrash();
  });

  const restoreWorkspace = (id: number) => runAdmin(async () => {
    await api.post(`/workspaces/${id}:restore`, {});
    await loadWorkspaces();
    const restored = useStore.getState().workspaces.find((item) => item.id === id);
    if (restored) await selectWorkspace(restored);
    await loadTrash();
  });

  // Cursor catch-up reads current state; delayed events never set job status.
  useEffect(() => {
    setIdxReady(null); setIndexError(''); setSyncError('');
    if (!currentCase || !user) return;
    const id = currentCase.id, userId = user.id;
    let active = true;
    let indexPending = false;
    let indexGeneration = 0;
    const isCurrent = () => active && useStore.getState().currentCase?.id === id && useStore.getState().user?.id === userId;
    const refreshIndex = async () => {
      if (!isCurrent() || indexPending) return;
      indexPending = true;
      const generation = ++indexGeneration;
      try {
        const value = await api.get<{ indexed: boolean }>(`/cases/${id}/index/status`);
        if (typeof value.indexed !== 'boolean') throw new Error('Invalid index status');
        if (isCurrent() && generation === indexGeneration) { setIdxReady(value.indexed); setIndexError(''); }
      } catch {
        if (isCurrent() && generation === indexGeneration) { setIdxReady(null); setIndexError('索引状态暂时无法核验，正在重试。'); }
      } finally { indexPending = false; }
    };
    const stop = watchCaseEvents(id, async update => {
      if (!isCurrent()) return null;
      void refreshIndex();
      try {
        const [watermark] = await Promise.all([useStore.getState().syncTree(update), useStore.getState().refreshCase(id)]);
        if (isCurrent()) {
          setSyncError('');
          window.dispatchEvent(new CustomEvent("vibeflow:case-reconciled", {detail: {caseId: id}}));
        }
        return watermark;
      } catch (error) {
        if (isCurrent()) {
          const denied = error instanceof ApiError && [401,403,404].includes(error.status);
          if (denied) {
            ++searchGeneration.current; ++indexGeneration;
            setHits(null); setSearching(false); setIdxReady(null);
            useStore.setState({ fileTree: [] });
          }
          setSyncError(denied ? '文件视图已不存在或无权访问，请核对工作区权限。' : '文件视图暂时无法刷新，正在重试。');
        }
        throw error;
      }
    });
    return () => { active = false; stop(); };
  }, [currentCase?.id, user?.id]);

  useEffect(() => {
    ++searchGeneration.current; setHits(null); setSearching(false); setSearchError('');
  }, [currentCase?.id, currentWs?.id, user?.id, searchScope]);

  const doSearch = async () => {
    if (!currentCase || !query.trim() || (searchScope === "workspace" && !currentWs)) return;
    const generation = ++searchGeneration.current, caseId = currentCase.id, userId = user?.id, workspaceId = currentWs?.id;
    const isCurrent = () => generation === searchGeneration.current && useStore.getState().currentCase?.id === caseId
      && useStore.getState().currentWs?.id === workspaceId && useStore.getState().user?.id === userId;
    setSearching(true);
    setSearchError('');
    try {
      const base = searchScope === "workspace"
        ? `/workspaces/${currentWs!.id}/search` : `/cases/${currentCase.id}/search`;
      const r = await api.get<{ hits: any[] }>(`${base}?q=${encodeURIComponent(query.trim())}`);
      if (isCurrent()) setHits(r.hits);
    } catch { if (isCurrent()) { setHits(null); setSearchError('搜索暂时不可用，请重试。'); } }
    finally { if (isCurrent()) setSearching(false); }
  };

  const openSearchHit = async (hit: { path: string; case_id?: number }) => {
    if (hit.case_id && hit.case_id !== currentCase?.id) {
      const target = cases.find((item) => item.id === hit.case_id);
      if (!target) return;
      await selectCase(target);
    }
    if (hit.case_id && useStore.getState().currentCase?.id !== hit.case_id) return;
    if (await openFromTree(hit.path)) { setHits(null); setQuery(""); }
  };

  const rebuildIndex = async () => {
    if (!currentCase || caseReadOnly) return;
    const generation = ++searchGeneration.current, caseId = currentCase.id, userId = user?.id;
    const isCurrent = () => generation === searchGeneration.current && useStore.getState().currentCase?.id === caseId && useStore.getState().user?.id === userId;
    setSearching(true);
    try {
      await api.post(`/cases/${currentCase.id}/index:rebuild`, {});
      if (isCurrent()) { setIdxReady(true); setIndexError(''); }
    } catch { if (isCurrent()) setIndexError('索引建立失败，请核对写入权限后重试。'); }
    finally { if (isCurrent()) setSearching(false); }
  };

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 8, color: "#cfd6e4", fontSize: 13 }}>
      {fileOpen && <div data-testid="project-file-open-status" className="vf-project-error" role={fileOpen.failed ? "alert" : "status"}
                        style={{ position: "sticky", top: 0, zIndex: 1, background: "var(--vf-panel)", paddingBlock: 4 }}>
        {fileOpen.message}
        {fileOpen.failed && <button type="button" onClick={() => void openFromTree(fileOpen.path, fileOpen.original)}>重试</button>}
        {fileOpen.failed && !fileOpen.original && /\.msh$/i.test(fileOpen.path) &&
          <button type="button" onClick={() => void openFromTree(fileOpen.path, true)}>打开原文件</button>}
      </div>}
      {viewRecoveryNotice && <p role="status" data-testid="view-recovery-notice" style={{ color: 'var(--vf-text)', fontSize: 14, lineHeight: 1.6, overflowWrap: 'anywhere' }}>{viewRecoveryNotice}</p>}
      {projectLoadError && <div role="alert" className="vf-project-error">{projectLoadError.message}
        <button type="button" onClick={() => void retryProjectLoad()}>重新加载</button>
      </div>}
      <div style={s.h}>工作区</div>
      <select
        aria-label="当前工作区"
        style={s.select}
        value={currentWs?.id ?? ""}
        onChange={(e) => {
          const w = workspaces.find((x) => x.id === Number(e.target.value));
          if (w) selectWorkspace(w);
        }}
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>{w.name}</option>
        ))}
      </select>
      <CreationForm key={user?.id} kind="工作区" create={createWorkspace} />

      <div style={s.row}>
        <button style={s.btn} disabled={!ownsWorkspace || adminBusy} title="重命名工作区" onClick={renameCurrentWorkspace}>重命名</button>
        <button style={s.btn} disabled={!ownsWorkspace || adminBusy} title="复制工作区" onClick={copyCurrentWorkspace}>复制</button>
        <button data-testid="workspace-share" style={s.btn} disabled={!currentWs || adminBusy} aria-expanded={sharingOpen}
                onClick={() => setSharingOpen(open => !open)}>组织授权</button>
        <button data-testid="workspace-delete" style={{ ...s.btn, background: "#7f1d1d" }}
                disabled={!ownsWorkspace || adminBusy} title="工作区移入回收站" onClick={deleteCurrentWorkspace}>删除</button>
        <button data-testid="trash-toggle" style={s.btn} disabled={adminBusy}
                onClick={() => setTrashOpen(!trashOpen)}>回收站</button>
      </div>
      {sharingOpen && currentWs && <ProjectAccess key={`${user?.id}:${currentWs.id}:${currentCase?.id ?? ''}`}
        workspace={currentWs.id} caseId={currentCase?.id} />}

      <div style={s.h}>算例</div>
      {cases.map((c: CaseInfo) => (
        <button type="button"
          key={c.id}
          data-case-name={c.name}
          aria-pressed={currentCase?.id === c.id}
          className="vf-project-case"
          onClick={() => selectCase(c)}
        >
          <span style={s.badge}>{c.workflow_state}</span>
          <span className="vf-project-name" title={c.name}>{c.name}</span>
          <span style={{ color: "var(--vf-muted)", marginLeft: "auto" }}>
            {STATE_LABEL[c.workflow_state] ?? ""}
          </span>
        </button>
      ))}
      <CreationForm key={`${user?.id}/${currentWs?.id}`} kind="算例" create={createCase} importZip={importCase}
        disabled={!currentWs || currentWs.shared || casesLoading || projectLoadError?.target === 'workspace'} />
      <div style={s.row}>
        <button style={s.btn} disabled={!currentCase || caseReadOnly || adminBusy}
                title="重命名算例" onClick={renameCurrentCase}>重命名</button>
        <button style={s.btn} disabled={!currentCase || !ownsWorkspace || !moveTargets.length || adminBusy}
                title="移动算例" aria-expanded={moveTarget !== null} onClick={() => { setAdminError(''); setMoveTarget(''); }}>移动</button>
        <button style={s.btn} disabled={!currentCase || !ownsWorkspace || adminBusy} title="复制算例" onClick={copyCurrentCase}>复制</button>
        <button data-testid="case-delete" style={{ ...s.btn, background: "#7f1d1d" }}
                disabled={!currentCase || !ownsWorkspace || adminBusy} title="算例移入回收站" onClick={deleteCurrentCase}>删除</button>
      </div>
      {moveTarget !== null && <form aria-label="移动算例" onSubmit={event => { event.preventDefault(); void moveCurrentCase(); }}>
        <label>目标工作区
          <select style={s.select} value={moveTarget} disabled={adminBusy} onChange={event => setMoveTarget(event.target.value)}>
            <option value="">选择目标工作区</option>
            {moveTargets.map(workspace => <option key={workspace.id} value={workspace.id}>
              {moveTargets.some(other => other.id !== workspace.id && other.name === workspace.name)
                ? `${workspace.name}（编号 ${workspace.id}）` : workspace.name}
            </option>)}
          </select>
        </label>
        <div style={s.row}>
          <button type="submit" style={s.btn} disabled={!moveTarget || adminBusy}>确认移动</button>
          <button type="button" style={s.btn} disabled={adminBusy} onClick={() => setMoveTarget(null)}>取消</button>
        </div>
      </form>}
      {adminError && <div role="alert" style={{ color: "#f87171", fontSize: 11, marginBottom: 6 }}>{adminError}</div>}
      {trashOpen && (
        <div data-testid="trash-panel" style={{ border: "1px solid #22304a", borderRadius: 6,
          padding: 6, marginTop: 6, maxHeight: 220, overflow: "auto" }}>
          <div style={{ color: "#8b93a7", fontSize: 11, marginBottom: 4 }}>回收站（仅恢复，不永久删除）</div>
          {trash.workspaces.map((item) => (
            <div key={`ws-${item.id}`} style={s.hitRow} data-trash-name={item.name}>
              <span>工作区：{item.name}</span>
              <button data-testid={`workspace-restore-${item.id}`} style={s.btn}
                      disabled={adminBusy} onClick={() => restoreWorkspace(item.id)}>恢复</button>
            </div>
          ))}
          {trash.cases.map((item) => (
            <div key={`case-${item.id}`} style={s.hitRow} data-trash-name={item.name}>
              <span>算例：{item.name}</span>
              <button data-testid={`case-restore-${item.id}`} style={s.btn}
                      disabled={adminBusy} onClick={() => restoreCase(item.id)}>恢复</button>
            </div>
          ))}
          {!trash.workspaces.length && !trash.cases.length &&
            <div style={{ color: "#667", fontSize: 11 }}>回收站为空</div>}
        </div>
      )}

      {currentCase && (
        <>
          {caseReadOnly && <p style={{ color: 'var(--vf-muted)', fontSize: 14 }}>只读算例</p>}
          {/* 当前算例或整个工作区的 FTS5 搜索 */}
          <select data-testid="search-scope" style={{ ...s.select, marginBottom: 4 }}
                  value={searchScope} onChange={(event) => {
                    ++searchGeneration.current; setSearchScope(event.target.value as "case" | "workspace"); setHits(null); setSearching(false);
                  }}>
            <option value="case">搜索当前算例</option>
            <option value="workspace">搜索整个工作区</option>
          </select>
          <div style={s.row}>
            <input style={s.input} placeholder="搜索文件名/内容…"
                   value={query} onChange={(e) => { ++searchGeneration.current; setQuery(e.target.value); setHits(null); setSearching(false); setSearchError(''); }}
                   onKeyDown={(e) => e.key === "Enter" && doSearch()} />
            <button style={s.btn} onClick={doSearch} disabled={searching || !query.trim()}>
              {searching ? "…" : "搜"}
            </button>
          </div>
          {syncError && <p role="status" data-testid="tree-sync-error" style={{ fontSize: 14, color: 'var(--vf-text)' }}>{syncError}</p>}
          {indexError && <p role="status" data-testid="index-status-error" style={{ fontSize: 14, color: 'var(--vf-text)' }}>{indexError}</p>}
          {searchError && <p role="alert" data-testid="search-error" style={{ fontSize: 14, color: 'var(--vf-text)' }}>{searchError}</p>}
          {searchScope === "case" && idxReady === false && (
            <div style={{ fontSize: 14, lineHeight: 1.5, color: 'var(--vf-text)', marginBottom: 4 }}>
              <p style={{ margin: '4px 0' }}>该算例尚未建立搜索索引。</p>
              {!caseReadOnly ? <button type="button" data-testid="index-rebuild" className="index-rebuild-action"
                style={{ ...s.btn, minHeight: 44, fontSize: 14, color: 'var(--vf-text)', background: 'var(--vf-panel)', border: '1px solid var(--vf-border)' }}
                onClick={rebuildIndex} disabled={searching}>{searching ? '正在建立索引…' : '建立搜索索引'}</button>
                : <p data-testid="index-readonly" style={{ margin: '4px 0' }}>请联系工作区所有者或编辑者建立索引。</p>}
            </div>
          )}
          {hits !== null && (
            <div style={{ border: "1px solid #22304a", borderRadius: 6, marginBottom: 6,
                          maxHeight: 220, overflow: "auto", background: "#10141c" }}>
              {hits.map((h) => (
                <div key={`${h.case_id ?? currentCase.id}:${h.path}:${h.snippet ?? ""}`}
                     data-testid="search-hit" data-case-id={h.case_id ?? currentCase.id} style={s.hitRow}
                     onClick={() => void openSearchHit(h)}>
                  <div style={{ fontSize: 12, color: "#7db4ff" }}>
                    📄 {h.case_name && <span style={{ color: "#9ab3d0" }}>[{h.case_name}] </span>}{h.path}
                  </div>
                  {h.snippet && (
                    <div style={{ fontSize: 11, color: "#8b93a7", whiteSpace: "nowrap",
                                  overflow: "hidden", textOverflow: "ellipsis" }}>{h.snippet}</div>
                  )}
                </div>
              ))}
              {!hits.length && <div style={{ fontSize: 11, color: "#667", padding: 6 }}>无命中</div>}
            </div>
          )}
          {!caseReadOnly && <><CaseFileActions key={`actions-${currentCase.id}`} caseId={currentCase.id} caseName={currentCase.name} onReviewUpload={identifier=>{
            const userId=useStore.getState().user?.id;
            if(userId)setManualSource({caseId:currentCase.id,userId,source:`uploads/${identifier}`,token:crypto.randomUUID()});
          }} />
          <ManualFileRecovery caseId={currentCase.id} sourceIntent={manualSource?.caseId===currentCase.id
            && manualSource.userId===useStore.getState().user?.id?manualSource:null} /></>}
          <TreeNodes key={`tree-${currentCase.id}`} nodes={tree} depth={0} onOpen={path => void openFromTree(path)} />
        </>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  h: { fontSize: 11, color: "#667", textTransform: "uppercase", margin: "12px 4px 4px" },
  select: { width: "100%", padding: 6, background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6 },
  row: { display: "flex", gap: 4, marginTop: 4 },
  input: { flex: 1, padding: "5px 8px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12, minWidth: 0 },
  btn: { padding: "5px 10px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer" },
  badge: { background: "#3b82f622", color: "#7db4ff", border: "1px solid #3b82f6", borderRadius: 4, fontSize: 11, width: 16, height: 16, display: "inline-flex", alignItems: "center", justifyContent: "center" },
};
