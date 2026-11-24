import MonacoEditor from "./MonacoEditor";
import "katex/dist/katex.min.css";
import { useEffect, useRef, useState } from "react";
import { registerFoamCompletion, type FoamCompletionItem } from "../utils/openfoamLang";
import { langOf } from "../utils/fileLanguage";
import { formatPath } from "../utils/pathRefs";
import MarkdownPreview from "./MarkdownPreview";
import { api } from "../api";
import { registerEditor, syncEditorPath } from "../shell/editorRef";
import { activatePanel, isViewerFile } from "../shell/dock";
import { captureProjectCase, useStore, type OpenFile } from "../store";

function BinaryDownload({ file }: { file: OpenFile }) {
  const request = useRef<AbortController | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "pending" | "success" | "error"; message: string } | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const download = async () => {
    const selection = captureProjectCase();
    if (!selection || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const isCurrent = () => !controller.signal.aborted && selection.isCurrent() &&
      useStore.getState().activeFile === file.path;
    setFeedback({ kind: "pending", message: "下载中…" });
    try {
      if (!file.download_url) {
        setFeedback({ kind: "error", message: "下载地址不可用，请重新打开文件。" });
        return;
      }
      const target = new URL(file.download_url, window.location.href);
      const platformFile = target.origin === window.location.origin && /^\/api\/v1\/cases\/\d+\/raw$/.test(target.pathname);
      const token = platformFile ? localStorage.getItem("vf_token") : null;
      const response = await fetch(target, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        credentials: platformFile ? "same-origin" : "omit",
        signal: controller.signal,
      });
      if (!isCurrent()) return;
      if (!response.ok) {
        const next = response.status === 401 ? "请重新登录后重试。" :
          response.status === 403 || response.status === 404 ? "请核对访问权限或重新打开文件后重试。" : "请稍后重试。";
        setFeedback({ kind: "error", message: `下载失败（${response.status}）。${next}` });
        return;
      }
      const blob = await response.blob();
      if (!isCurrent()) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      try {
        anchor.href = url;
        anchor.download = file.path.split("/").pop() || "download";
        document.body.appendChild(anchor);
        anchor.click();
      } finally {
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      setFeedback({ kind: "success", message: "已开始下载" });
    } catch {
      if (isCurrent()) setFeedback({ kind: "error", message: "下载失败，请检查连接后重试。" });
    } finally {
      request.current = null;
    }
  };

  const pending = feedback?.kind === "pending";
  return (
    <div style={s.empty} data-testid="binary-file-download">
      <div>二进制文件（{(file.size / 1024).toFixed(1)} KB）</div>
      <button type="button" style={s.save} disabled={pending} onClick={() => void download()}>下载</button>
      {feedback?.kind === "error"
        ? <p role="alert" className="vf-project-error">{feedback.message}</p>
        : <span role="status" style={{ color: "var(--vf-text)" }}>{feedback?.message}</span>}
    </div>
  );
}

export default function EditorPanel() {
  const { openFiles, activeFile, editContent, saveFile, saveAll, closeAll,
    setActiveFile, closeFile, currentCase, pathFormat, theme, autoPreviewHandoffs,
    inspectServerVersion, resolveFileConflict } = useStore();
  const [preview, setPreview] = useState(false);
  const [note, setNote] = useState("");
  const [savingAll, setSavingAll] = useState(false);
  const [saveAllError, setSaveAllError] = useState("");
  const [checkingServer, setCheckingServer] = useState(false);
  const [cursorLine, setCursorLine] = useState(1);
  const f = activeFile ? openFiles[activeFile] : null;
  const paths = Object.keys(openFiles);
  const dirtyCount = paths.filter((path) => openFiles[path].dirty && openFiles[path].kind === "text").length;
  const isMd = f?.path.toLowerCase().endsWith(".md") ?? false;
  const autoPreviewError = currentCase && autoPreviewHandoffs[currentCase.id]?.error;
  const previewWarning = autoPreviewError && (
    <div role="status" className="vf-project-error" data-testid="editor-auto-preview-warning">
      几何脚本已保存，自动预览未启动。{autoPreviewError.message}
      <button type="button" style={s.toggle} onClick={() => activatePanel("geometry")}>打开几何预览</button>
    </div>
  );

  useEffect(() => {
    syncEditorPath(f?.path ?? null);
    setNote("");
  }, [f?.path, currentCase?.id]);

  useEffect(() => { setSavingAll(false); setSaveAllError(""); }, [currentCase?.id]);

  const openEditors = paths.length > 0 && (
    <div data-testid="open-editors" data-dirty-count={dirtyCount} data-saving-all={savingAll} style={s.tabsBar}>
      <div style={s.tabs}>
        {paths.map((path) => (
          <div key={path} data-editor-path={path} data-active={path === activeFile}
               data-dirty={Boolean(openFiles[path].dirty)}
               style={{ ...s.tab, ...(path === activeFile ? s.tabActive : {}) }} title={path}>
            <button type="button" aria-pressed={path === activeFile} style={s.tabSelect}
                    onClick={() => { setActiveFile(path); if (isViewerFile(path)) activatePanel("viewer"); }}>
              {path.split("/").pop()}{openFiles[path].dirty ? " ●" : ""}
            </button>
            <button type="button" style={s.tabClose} title="关闭文件" aria-label={`关闭 ${path}`}
                    onClick={() => {
                      if (!openFiles[path].dirty || window.confirm("此文件有未保存的编辑。确定关闭并丢弃草稿？")) closeFile(path);
                    }}>×</button>
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 4 }}>
        <button style={s.toggle} disabled={!dirtyCount || savingAll}
                onClick={async () => {
                  const selection = captureProjectCase();
                  setSavingAll(true);
                  setSaveAllError("");
                  try {
                    await saveAll();
                    if (selection?.isCurrent()) setNote("全部已保存");
                  } catch (error) {
                    if (selection?.isCurrent()) setSaveAllError(error instanceof Error ? error.message : String(error));
                  } finally {
                    if (selection?.isCurrent()) setSavingAll(false);
                  }
                }}>{savingAll ? "保存中…" : "全部保存"}</button>
        <button style={s.toggle} onClick={() => {
          if (dirtyCount && !window.confirm(`有 ${dirtyCount} 个未保存文件，确定全部关闭？`)) return;
          closeAll();
        }}>全部关闭</button>
      </div>
    </div>
  );

  const doSave = async (path: string) => {
    const selection = captureProjectCase();
    let r;
    try { r = await saveFile(path); }
    catch { if (selection?.isCurrent()) setNote(""); return; }
    if (!r || !selection?.isCurrent()) return;
    setNote(useStore.getState().openFiles[path]?.dirty ? "已保存提交时的内容，后续编辑尚未保存"
      : r.auto_preview_job ? `已保存 · 几何重建作业 #${r.auto_preview_job} 已触发` : "已保存");
    if (r.auto_preview_job) activatePanel("geometry");
    setTimeout(() => setNote(""), 4000);
  };

  const copyPath = async (storage: boolean) => {
    const selection = captureProjectCase();
    if (!f || !selection) return;
    try {
      let text = f.path;
      if (storage) {
        const { storage_prefix } = await api.get<{ storage_prefix?: string }>(`/cases/${selection.caseId}`);
        if (!selection.isCurrent() || useStore.getState().activeFile !== f.path) return;
        if (!storage_prefix) throw new Error("无法读取存储路径，可复制相对路径。");
        text = `${storage_prefix}/${f.path}`;
      }
      await navigator.clipboard.writeText(formatPath(text, pathFormat));
      if (selection.isCurrent()) setNote(storage ? "已复制存储路径" : "已复制相对路径");
    } catch (error) {
      if (selection.isCurrent()) setNote(error instanceof Error ? error.message : String(error));
    }
  };

  const addToChat = () => {
    if (!f) return;
    const reference = `@${f.path}`;
    useStore.getState().updateAgentDraft(draft => {
      if (draft.input.split(/\s+/).includes(reference)) return draft;
      const input = draft.input.trimEnd();
      return { ...draft, input: `${input}${input ? " " : ""}${reference} ` };
    });
    activatePanel("agent");
  };

  if (!f) {
    return (
      <div style={s.page}>{openEditors}{previewWarning}
        <div style={s.empty}>
          <div style={{ fontSize: 40 }}>🌊</div>
          <div>从左侧文件树打开一个文件</div>
          <div style={{ fontSize: 12, color: "#667" }}>caseDict · Python · Markdown 支持编辑保存；.md 支持预览</div>
        </div>
      </div>
    );
  }
  if (f.kind === "binary") {
    return (
      <div style={s.page}>{openEditors}{previewWarning}
        <BinaryDownload key={f.editorId ?? f.path} file={f} />
      </div>
    );
  }
  return (
    <div style={s.page}>{openEditors}{previewWarning}
      {saveAllError && <div role="alert" className="vf-project-error">{saveAllError}</div>}
      <div style={s.bar}>
        <span style={{ color: "#9ab", fontSize: 12 }}>{f.path}{f.dirty ? " ●" : ""}</span>
        <span data-testid="editor-cursor-line" title="Monaco 当前光标行" style={{ color: "#667", fontSize: 11 }}>L{cursorLine}</span>
        {note && <span style={{ color: "#16a34a", fontSize: 12 }}>{note}</span>}
        <div style={{ display: "flex", gap: 6 }}>
          <button style={s.toggle} onClick={() => copyPath(true)}>复制路径</button>
          <button style={s.toggle} onClick={() => copyPath(false)}>复制相对路径</button>
          <button style={s.toggle} onClick={addToChat}>加入聊天</button>
          {isMd && (
            <button data-testid="markdown-preview-toggle" style={s.toggle} onClick={() => setPreview(!preview)}>
              {preview ? "源码" : "预览"}
            </button>
          )}
          <button style={s.save} disabled={!f.dirty || f.saving} onClick={() => doSave(f.path)}>
            {f.saving ? "保存中…" : "保存 (Ctrl+S)"}
          </button>
        </div>
      </div>
      {f.saveError && <div role="alert" data-testid="editor-save-error" style={s.saveError}>
        <div>{f.saveError}</div>
        <button style={s.toggle} disabled={checkingServer || f.saving} onClick={async () => {
          setCheckingServer(true);
          try { await inspectServerVersion(f.path); }
          catch (error) { setNote(error instanceof Error ? error.message : String(error)); }
          finally { setCheckingServer(false); }
        }}>{checkingServer ? "读取中…" : "查看服务器版本"}</button>
      </div>}
      {f.serverVersion !== undefined && <div data-testid="editor-server-version" style={s.serverVersion}>
        <div>{f.serverVersion ? "服务器当前内容（本地草稿保留在编辑区）" : "服务器文件已删除。"}</div>
        {f.serverVersion && <pre style={{maxHeight:180, overflow:"auto", margin:"6px 0", fontSize:12}}>{f.serverVersion.content}</pre>}
        <div style={{display:"flex", flexWrap:"wrap", gap:6}}>
          <button style={s.toggle} disabled={f.saving} onClick={() => resolveFileConflict(f.path, true)}>
            {f.serverVersion ? "保留草稿，基于此版本继续编辑" : "保留草稿，按新文件保存"}
          </button>
          <button style={s.toggle} disabled={f.saving} onClick={() => {
            const message = f.serverVersion ? "使用服务器版本会放弃此文件的本地草稿，是否继续？" : "确定放弃此文件的草稿并关闭编辑器？";
            if (window.confirm(message)) resolveFileConflict(f.path, false);
          }}>{f.serverVersion ? "使用服务器版本" : "放弃草稿并关闭"}</button>
        </div>
      </div>}
      {isMd && preview ? (
        <MarkdownPreview key={f.editorId} source={f.content ?? ""} style={s.md}
          assets={f.path.startsWith("E-report/mineru/") && currentCase ? { caseId: currentCase.id, documentPath: f.path } : undefined} />
      ) : (
        <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
          <MonacoEditor
            theme={theme === "light" ? "vs" : "vs-dark"}
            language={langOf(f.path, f.language)}
            value={f.content ?? ""}
            onChange={(v) => editContent(f.path, v ?? "")}
            onMount={(editor, monaco) => {
              registerEditor(editor);  // 供 texo 公式识别插入 LaTeX
              setCursorLine(editor.getPosition()?.lineNumber ?? 1);
              editor.onDidChangeCursorPosition((event) => setCursorLine(event.position.lineNumber));
              // OpenFOAM 字典语言 + 补全（幂等注册）
              registerFoamCompletion(monaco, () => {
                const { activeFile, currentCase } = useStore.getState();
                return { path: activeFile, caseId: currentCase?.id };
              }, (query) => {
                const parameters = new URLSearchParams({ q: query.q });
                if (query.parent) parameters.set("parent", query.parent);
                if (query.context) parameters.set("context", query.context);
                if (query.kind) parameters.set("kind", query.kind);
                return api.get<{ ready: boolean; items: FoamCompletionItem[] }>(`/completion/openfoam?${parameters}`);
              });
              // editor 实例级快捷键（addKeybinding 全局 API 不存在于当前 monaco 版本）
              editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
                const p = useStore.getState().activeFile;
                if (p) doSave(p);
              });
            }}
            options={{ fontSize: 13, minimap: { enabled: false }, automaticLayout: true }}
          />
        </div>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  page: { height: "100%", display: "flex", flexDirection: "column" },
  saveError: {padding:"8px 12px", color:"#fca5a5", background:"#291b21", fontSize:14, lineHeight:1.5, overflowWrap:"anywhere", flexShrink:0},
  serverVersion: {padding:"8px 12px", color:"#d7dce5", background:"#10141c", fontSize:14, maxHeight:"35%", flexShrink:0, overflow:"auto"},
  tabsBar: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6, borderBottom: "1px solid #2a2f3a", padding: "2px 4px" },
  tabs: { display: "flex", overflowX: "auto", gap: 2, flex: 1 },
  tab: { display: "flex", alignItems: "center", gap: 4, padding: "3px 5px", color: "#9ab", fontSize: 12, whiteSpace: "nowrap", borderRadius: 4 },
  tabActive: { background: "#1c2740", color: "#7db4ff" },
  tabSelect: { background: "transparent", color: "inherit", border: 0, padding: 0, font: "inherit", cursor: "pointer" },
  tabClose: { background: "transparent", color: "#8b93a7", border: 0, cursor: "pointer", padding: 2 },
  empty: { height: "100%", display: "flex", flexDirection: "column", gap: 8, alignItems: "center", justifyContent: "center", color: "#8b93a7" },
  bar: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 8px", borderBottom: "1px solid #2a2f3a" },
  save: { padding: "3px 12px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12 },
  toggle: { padding: "3px 12px", background: "#1a1e27", color: "#9ab", border: "1px solid #2a2f3a", borderRadius: 5, cursor: "pointer", fontSize: 12 },
  md: { flex: 1, overflow: "auto", padding: "12px 20px", color: "#d7dce5", fontSize: 14, lineHeight: 1.7 },
};

