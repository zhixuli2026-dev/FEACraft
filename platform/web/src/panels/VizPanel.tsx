import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { api } from "../api";
import { beginFileOpen, captureProjectCase, useStore } from "../store";
import type { FileOpenIntent } from "../store";
import NativeSessionHistory from "./NativeSessionHistory";
import ParaViewMacros from "./ParaViewMacros";
import ParaViewCatalog from "./ParaViewCatalog";
import VizCollaboration from "./VizCollaboration";
import { nativeSessionClosedEvent } from "../utils/nativeSessionHistory";
import type { NativeSessionClosed } from "../utils/nativeSessionHistory";

interface VizSession {
  id: number; run_dir: string; file_path?: string | null; port: number; url: string;
  alive: boolean | null; owner_available: boolean; started_at: number;
  engine?: Engine;
}
type Engine = "trame" | "paraview";

/** 会话 iframe 走主后端鉴权反代（kernel 裸端口不进浏览器网络） */
async function proxyUrl(s: { id: number }): Promise<string> {
  const { url } = await api.post<{ url: string }>("/connection-tickets", { scope: "viz", session_id: String(s.id) });
  return url;
}

/** Both rendering engines use the case-owned session gateway. */
export default function VizPanel() {
  const { currentCase, fileTree, user, filePreviewRequest } = useStore();
  const [runDir, setRunDir] = useState("");
  const [sessions, setSessions] = useState<VizSession[]>([]);
  const [activeUrl, setActiveUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [engine, setEngine] = useState<Engine>("trame");
  const [engines, setEngines] = useState<Engine[]>(["trame"]);
  const [pythonConsole, setPythonConsole] = useState(false);
  const launch = useRef<FileOpenIntent | null>(null);
  const listSequence = useRef(0);
  const viewSequence = useRef(0);
  const viewport = useRef<HTMLDivElement | null>(null);
  const iframe = useRef<HTMLIFrameElement | null>(null);
  const canPresent = useStore(state => Boolean(state.currentWs && state.currentWs.access_role !== "viewer"));

  // 从文件树推导求解目录（含 system/controlDict 的 C-run/* 或算例根）
  const runDirs = Array.from(new Set(
    fileTree
      .filter((t) => t.path === "system/controlDict" || t.path.endsWith("/system/controlDict"))
      .map((t) => t.path === "system/controlDict" ? "." : t.path.slice(0, -"/system/controlDict".length))
      .filter((p) => p.startsWith("C-run/") || !p.includes("/")),
  ));
  const effectiveRunDir = runDir || runDirs[0] || "";

  const reload = async () => {
    const selection = captureProjectCase();
    if (!selection) return;
    const sequence = ++listSequence.current;
    try {
      const result = await api.get<VizSession[]>(`/cases/${selection.caseId}/viz:sessions`);
      if (selection.isCurrent() && sequence === listSequence.current) setSessions(result);
    } catch { /* The interactive start and stop handlers report their own errors. */ }
  };
  useEffect(() => {
    launch.current = null;
    ++viewSequence.current;
    setSessions([]); setActiveUrl(""); setErr(""); setRunDir(""); setBusy(false);
    setEngine("trame"); setEngines(["trame"]); setPythonConsole(false);
    const controller = new AbortController();
    const selection = captureProjectCase();
    if (selection) void api.get<{ available: Engine[]; python_console: boolean }>(`/cases/${selection.caseId}/viz:engines`, undefined, controller.signal)
      .then(value => {
        if (controller.signal.aborted || !selection.isCurrent()) return;
        setEngines(value.available);
        setPythonConsole(value.python_console);
      }).catch(() => {});
    void reload();
    const onSessionClosed = (event: Event) => {
      const session = (event as CustomEvent<NativeSessionClosed>).detail;
      if (!session || session.kind !== 'viz' || session.case_id !== currentCase?.id || session.userId !== user?.id) return;
      setActiveUrl(current => current.includes(`/viz-proxy/${session.id}/`) ? "" : current);
      void reload();
    };
    window.addEventListener(nativeSessionClosedEvent, onSessionClosed);
    return () => { controller.abort(); ++listSequence.current; window.removeEventListener(nativeSessionClosedEvent, onSessionClosed); };
  }, [currentCase?.id, user?.id]);

  const start = async (intent: FileOpenIntent | null, target: { runDir?: string; filePath?: string; reopen?: number }) => {
    if (!intent?.isCurrent() || (!target.filePath && !target.runDir && !target.reopen)) return;
    const sequence = ++viewSequence.current;
    launch.current = intent;
    setBusy(true); setErr("");
    try {
      const session = target.reopen
        ? await api.post<VizSession>(`/viz:sessions/${target.reopen}:reopen`, {})
        : await api.post<VizSession>(`/cases/${intent.caseId}/viz:sessions`,
          target.filePath ? { file_path: target.filePath, engine } : { run_dir: target.runDir, engine });
      const url = intent.isCurrent() ? await proxyUrl(session) : "";
      if (intent.isCurrent() && url) {
        if (sequence === viewSequence.current) {
          setActiveUrl(url);
          if (target.reopen) requestAnimationFrame(() => {
            if (intent.isCurrent()) viewport.current?.scrollIntoView({ block: "nearest" });
          });
        }
        await reload();
      } else if (useStore.getState().user?.id === intent.userId) {
        // The superseded launch created its own session. Close only that session.
        try { await api.del(`/viz:sessions/${session.id}`); } catch { /* The native reaper owns retained sessions. */ }
      }
    } catch (error) {
      if (intent.isCurrent()) setErr(`可视化启动失败。${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (launch.current === intent) { launch.current = null; setBusy(false); }
    }
  };

  useEffect(() => {
    if (filePreviewRequest?.kind !== "viz" || useStore.getState().filePreviewRequest !== filePreviewRequest) return;
    useStore.setState({ filePreviewRequest: null });
    void start(filePreviewRequest.intent, filePreviewRequest);
  }, [filePreviewRequest]);

  useEffect(() => {
    const sid = Number(activeUrl.match(/^\/viz-proxy\/(\d+)\//)?.[1]);
    if (!sid) return;
    const renew = setInterval(() => { void proxyUrl({ id: sid }).catch(() => {}); }, 240_000);
    return () => clearInterval(renew);
  }, [activeUrl]);

  const open = async (session: { id: number }) => {
    const selection = captureProjectCase();
    if (!selection) return;
    const sequence = ++viewSequence.current;
    try {
      const url = await proxyUrl(session);
      if (selection.isCurrent() && sequence === viewSequence.current) setActiveUrl(url);
    } catch (error) {
      if (selection.isCurrent() && sequence === viewSequence.current)
        setErr(error instanceof Error ? error.message : "无法授权可视化连接");
    }
  };

  const kill = async (sid: number) => {
    const selection = captureProjectCase();
    const attached = activeUrl.startsWith(`/viz-proxy/${sid}/`) ? activeUrl : "";
    if (attached) flushSync(() => setActiveUrl(""));
    try {
      await api.del(`/viz:sessions/${sid}`);
      if (!selection?.isCurrent()) return;
      setActiveUrl(current => current.includes(`/viz-proxy/${sid}/`) ? "" : current);
      await reload();
    } catch (error) {
      if (!selection?.isCurrent()) return;
      if (attached) setActiveUrl(current => current || attached);
      setErr(`会话关闭失败。${error instanceof Error ? error.message : String(error)}`);
    }
  };

  if (!currentCase)
    return <div style={s.hint}>选择一个算例后启动可视化</div>;

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 13,
                  color: "#d7dce5", overflow: "auto" }}>
      <div style={s.bar}>
        {engines.length > 1 && <select style={s.select} aria-label="渲染引擎" value={engine} disabled={busy}
          onChange={event => setEngine(event.target.value as Engine)}>
          <option value="trame">VTK 交互会话</option><option value="paraview">ParaView 原生工作区</option>
        </select>}
        <select style={s.select} value={effectiveRunDir}
                onChange={(e) => setRunDir(e.target.value)}>
          {runDirs.map((d) => <option key={d} value={d}>{d === "." ? "算例根目录" : d}</option>)}
          {!runDirs.length && <option value="">（无求解目录——先跑 C 阶段求解）</option>}
        </select>
        <button style={s.btn} onClick={() => void start(beginFileOpen(), { runDir: effectiveRunDir })} disabled={!effectiveRunDir || busy}>
          {busy ? "启动中（模型加载较慢）…" : "▶ 启动可视化会话"}
        </button>
        {sessions.filter((x) => x.owner_available && x.alive === true).map((x) => (
          <span key={x.id} style={s.chip}>
            #{x.id} {x.engine === "paraview" ? "ParaView · " : ""}{x.file_path ?? x.run_dir}
            <button style={s.chipBtn} onClick={() => void open(x)}>打开</button>
            <button style={s.chipBtn} aria-label={`关闭可视化会话 ${x.id}`} onClick={() => void kill(x.id)}>✕</button>
          </span>
        ))}
        {err && <span role="alert" data-testid="viz-start-error" className="vf-project-error">{err}</span>}
      </div>
      {user && <ParaViewMacros key={`macros-${user.id}:${currentCase.id}`} userId={user.id} caseId={currentCase.id} />}
      {user && <ParaViewCatalog key={"catalog-" + user.id + ":" + currentCase.id} caseId={currentCase.id} pythonConsole={pythonConsole}
        onView={filePath => void start(beginFileOpen(), { filePath })} />}
      {user && <NativeSessionHistory key={`history-${user.id}:${currentCase.id}`} userId={user.id} caseId={currentCase.id} initialKind="viz"
        onVizSelect={id => void open({ id })} vizOpening={busy}
        onVizReopen={id => void start(beginFileOpen(), { reopen: id })} />}
      {user && <VizCollaboration key={`sharing-${user.id}:${currentCase.id}`} caseId={currentCase.id} userId={user.id}
        canPresent={canPresent} sessionId={Number(activeUrl.match(/^\/viz-proxy\/(\d+)\//)?.[1]) || null} iframe={iframe} />}
      <div ref={viewport} style={{ flex: 1, minHeight: activeUrl ? 300 : 0, background: "#0b0d12" }}>
        {activeUrl ? (
          // A different renderer owns a new document. Removing the previous
          // iframe avoids navigating it while its trame observers are active.
          <iframe key={activeUrl.split("?")[0]} ref={iframe} src={activeUrl} title="可视化"
                  style={{ width: "100%", height: "100%", border: "none" }} />
        ) : (
          <div style={s.hint}>选择求解目录或打开结果文件，启动可视化会话。</div>
        )}
      </div>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  hint: { padding: 16, color: "#8b93a7", lineHeight: 1.8 },
  bar: { display: "flex", gap: 8, padding: 8, borderBottom: "1px solid #2a2f3a",
         alignItems: "center", flexWrap: "wrap" },
  select: { padding: "4px 6px", background: "#0f1115", color: "#e8eaf0",
            border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  btn: { padding: "5px 14px", background: "#2563eb", color: "#fff", border: "none",
         borderRadius: 6, cursor: "pointer", fontSize: 12 },
  chip: { display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11,
          color: "#9ab3d0", background: "#141a24", border: "1px solid #22304a",
          borderRadius: 5, padding: "2px 6px" },
  chipBtn: { background: "transparent", color: "#7db4ff", border: "none",
             cursor: "pointer", fontSize: 11, padding: "0 3px" },
};
