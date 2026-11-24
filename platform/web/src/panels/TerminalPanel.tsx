import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { captureProjectCase, currentAgentDraft, useStore } from "../store";
import { activatePanel } from "../shell/dock";
import { TerminalOutputQueue, TerminalSgrStream } from "../utils/terminalOutput";
import { ImePunctuationGate } from "../utils/ime";
import { commandForCliAgent } from "../utils/cliProfiles";
import { sessionHistoryRequest } from "../utils/nativeSessionHistory";
import TerminalRecovery from "./TerminalRecovery";
import NativeSessionHistory from "./NativeSessionHistory";
import TerminalSelectionPreview from "./TerminalSelectionPreview";
import { maxMessageImages, readImageFiles } from "./ChatImages";
import { captureTerminalScreenshot } from "./terminalScreenshot";
import { terminalPreviewLimit } from "../utils/terminalSelectionPreview";
import type { CaseDictSnapshot } from "../utils/useCaseDraft";
import { cliContractsChanged } from "../components/CliContracts";
import "./TerminalPanel.css";

type TerminalShell = "cmd" | "wsl" | "wsl-sandbox";
const pythonConsoleCommand = `vf_python=python3; if [ -x /opt/vibeflow-paraview-5.11.2/bin/pvpython ]; then vf_python=/opt/vibeflow-paraview-5.11.2/bin/pvpython; fi; exec "$vf_python" -u -i -c 'import importlib.util, signal; from pathlib import Path; simple = importlib.import_module("paraview.simple") if importlib.util.find_spec("paraview") else None; signal.signal(signal.SIGINT, signal.default_int_handler); case_dir = Path.cwd(); print("Python console | case_dir =", case_dir)'`;
const pythonSessionKey = (scope: string, id: string) => `vf-python-console:${scope}:${id}`;

interface TermSession {
  id: string; shell: TerminalShell; case_id: number | null; alive: boolean | null;
  phase: string; cleanup_verified: boolean | null; owner_available: boolean;
  finalizing: boolean; completion_pending: boolean; completion_retained: boolean;
  actual_stop_observed: boolean | null; close_error: string | null;
}

function sessionState(session: TermSession) {
  if (session.phase === "starting") return "正在启动…";
  if (session.finalizing) return session.completion_pending && session.actual_stop_observed === true
    ? "进程已停止，正在保存关闭结果…" : "正在关闭…";
  if (session.completion_pending) return session.actual_stop_observed === true
    ? "进程已停止，关闭结果待保存" : "关闭结果待恢复";
  if (session.cleanup_verified === true) return session.phase === "failed" ? "已停止，收尾有错误" : "已关闭";
  if (session.close_error) return "关闭未完成，请重试";
  if (session.alive === true) return "运行中";
  return "状态待核验";
}

function closeError(failure: unknown) {
  if (failure instanceof ApiError) {
    if (failure.code === "completion_owner_alive") return "所属实例仍在处理关闭，请稍后重试保存结果。";
    if (failure.code === "completion_owner_exit_unverified") return "暂时无法核验所属实例是否退出，请保留会话并稍后重试。";
    if (failure.code === "completion_witness_unverifiable" || failure.code === "completion_binding_changed")
      return "保留的关闭结果未通过核验，请在会话历史中登记人工核查。";
    if (failure.status === 404 || failure.status === 403) return "会话已不可访问，请刷新会话列表。";
    if (failure.code === "terminal_cleanup_incomplete") return "关闭结果尚未保存，请重试；已停止的命令不会重新执行。";
  }
  return "关闭请求未能完成，请刷新状态后重试。";
}
type ProfileEnvMap = Record<string, Record<string, string>>;
const providerLabels = { kimi: "Kimi", opencode: "OpenCode 2", claude: "Claude Code", codex: "Codex Responses" };
const claudeProviderSupported = (row: { provider: string }) => ["anthropic", "kimi-coding", "deepseek"].includes(row.provider);
const codexProviderSupported = (row: { provider: string }) => !["anthropic", "kimi-coding"].includes(row.provider);

/** 终端面板：xterm.js + WS PTY + 多标签会话 + CLI agent 启动器（claude/opencode 等） */
export default function TerminalPanel() {
  const { currentCase, user, theme: appTheme, pythonConsoleRequest } = useStore();
  const caseId = currentCase?.id;
  const revision = currentCase?.dict_revision;
  const scope = `${user?.id ?? "none"}:${caseId ?? "unbound"}`;
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [shellChoice, setShellChoice] = useState<{scope: string; shell: TerminalShell} | null>(null);
  const [shellDefault, setShellDefault] = useState<{scope: string; revision?: number; shell?: TerminalShell}>();
  const configuredShell = shellDefault?.scope === scope && shellDefault.revision === revision ? shellDefault.shell : undefined;
  const shell = shellChoice?.scope === scope ? shellChoice.shell : configuredShell ?? shellChoice?.shell ?? "cmd";
  const [sessionRows, setSessions] = useState<TermSession[]>([]);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [attachment, setAttachment] = useState<{ scope: string; id: string; shell: typeof shell; command: string; providerId?: number; harness?: string } | null>(null);
  const [readyAttachment, setReadyAttachment] = useState<typeof attachment>(null);
  const [capturingAttachment, setCapturingAttachment] = useState<typeof attachment>(null);
  const [hasSelection, setHasSelection] = useState(false);
  const [preview, setPreview] = useState<{ origin: typeof attachment; text: string; truncated: boolean } | null>(null);
  const previewButton = useRef<HTMLButtonElement>(null);
  const currentAttachment = useRef(attachment); currentAttachment.current = attachment;
  const [sessionScope, setSessionScope] = useState(scope);
  const sessions = sessionScope === scope ? sessionRows : [];
  const activeSess = attachment?.scope === scope ? activeSession : null;
  const pythonActive = attachment?.scope === scope && (attachment.command === pythonConsoleCommand ||
    Boolean(activeSess && sessionStorage.getItem(pythonSessionKey(scope, activeSess))));
  const currentScope = useRef(scope); currentScope.current = scope;
  const mounted = useRef(true);
  const loadSequence = useRef(0);
  const sessionRead = useRef<{ scope: string; controller: AbortController; done: Promise<void> } | null>(null);
  const requestedClose = useRef(new Set<string>());
  const dismissed = useRef(new Set<string>());
  const [closeBusy, setCloseBusy] = useState<Set<string>>(new Set());
  const closeInFlight = useRef(new Set<string>());
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [agentTransferError, setAgentTransferError] = useState("");
  const [notice, setNotice] = useState("");
  const [resizeNotice, setResizeNotice] = useState("");
  const retryResizeRef = useRef<(() => void) | null>(null);
  const activeSessionRef = useRef(activeSess); activeSessionRef.current = activeSess;
  useEffect(() => {
    if (!caseId) return;
    let active = true;
    api.get<CaseDictSnapshot>(`/cases/${caseId}/caseDict`).then(result => {
      if (active) setShellDefault({scope, revision, shell: result.interactive_defaults?.shell});
    }).catch(() => {
      if (active) setShellDefault({scope, revision});
    });
    return () => { active = false; };
  }, [caseId, revision, scope]);
  const setActiveSess = useCallback((id: string | null, selectedShell = shell, command = "", providerId?: number, harness?: string) => {
    setActiveSession(id);
    setAttachment(id === null ? null : { scope, id, shell: selectedShell, command, providerId, harness });
    setPreview(null);
    setActionError(""); setAgentTransferError(""); setNotice("");
  }, [scope, shell]);
  useEffect(() => {
    if (!pythonConsoleRequest || pythonConsoleRequest.caseId !== caseId || pythonConsoleRequest.userId !== user?.id) return;
    if (useStore.getState().pythonConsoleRequest !== pythonConsoleRequest) return;
    useStore.getState().requestPythonConsole(null);
    setActiveSess("", "wsl-sandbox", pythonConsoleCommand);
  }, [caseId, user?.id, pythonConsoleRequest, setActiveSess]);
  const [quickOpen, setQuickOpen] = useState(false);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const recoveryButton = useRef<HTMLButtonElement>(null);
  const recoveryDialog = useRef<HTMLDialogElement>(null);
  const [quickText, setQuickText] = useState("");
  const [cliAgents, setCliAgents] = useState<Record<string, string>>({});
  const [cliProfile, setCliProfile] = useState("default");
  const [profileEnvs, setProfileEnvs] = useState<ProfileEnvMap>({});
  const [kimiProfiles, setKimiProfiles] = useState<{ id: number; name: string; provider: string; default_model: string; shared?: boolean; has_key: boolean }[]>([]);
  const [kimiProvider, setKimiProvider] = useState<string>();
  const [opencodeProvider, setOpencodeProvider] = useState<string>();
  const [claudeProvider, setClaudeProvider] = useState("");
  const [codexProvider, setCodexProvider] = useState<string>();
  const [contract, setContract] = useState<{ scope: string; selected: string; provider: string }>();
  const [providerError, setProviderError] = useState("");
  const [providerLoading, setProviderLoading] = useState(false);
  const providerSequence = useRef(0);
  const reloadProviders = useCallback(async () => {
    if (!user) return;
    const userId = user.id, sequence = ++providerSequence.current;
    const current = () => mounted.current && useStore.getState().user?.id === userId && sequence === providerSequence.current;
    setProviderLoading(true);
    try {
      const rows = await api.get<typeof kimiProfiles>("/provider-profiles");
      if (!current()) return;
      const owned = rows.filter(row => !row.shared && row.has_key && row.default_model);
      setKimiProfiles(owned); setProviderError("");
      setKimiProvider(previous => !previous || owned.some(row => String(row.id) === previous) ? previous : undefined);
      setOpencodeProvider(previous => !previous || owned.some(row => String(row.id) === previous) ? previous : undefined);
      setClaudeProvider(previous => owned.some(row => String(row.id) === previous && claudeProviderSupported(row)) ? previous : "");
      setCodexProvider(previous => !previous || owned.some(row => String(row.id) === previous && codexProviderSupported(row)) ? previous : undefined);
    } catch { if (current()) setProviderError("API 配置读取失败，请重新读取。"); }
    finally { if (current()) setProviderLoading(false); }
  }, [user]);
  useEffect(() => {
    setKimiProfiles([]); setKimiProvider(undefined); setOpencodeProvider(undefined); setClaudeProvider(""); setCodexProvider(undefined); setProviderError("");
    void reloadProviders();
    return () => { providerSequence.current++; };
  }, [reloadProviders]);
  useEffect(() => {
    setContract(undefined);
    setKimiProvider(undefined); setOpencodeProvider(undefined); setCodexProvider(undefined);
    if (!caseId || !user) return;
    const controller = new AbortController();
    let revision = 0;
    const refresh = () => {
      const expected = ++revision;
      void api.get<{applied: boolean; selected: string | null; provider_profile_id: number | null}>(
        `/cases/${caseId}/cli-contract`, undefined, controller.signal).then(state => {
        if (controller.signal.aborted || revision !== expected) return;
        setContract(state.applied && state.selected && state.provider_profile_id
          ? {scope, selected:state.selected, provider:String(state.provider_profile_id)} : undefined);
      }).catch(() => {});
    };
    refresh();
    window.addEventListener(cliContractsChanged, refresh);
    return () => { controller.abort(); window.removeEventListener(cliContractsChanged, refresh); };
  }, [scope, caseId, user?.id]);
  const selectedProviders: Record<string, string | undefined> = {
    kimi:kimiProvider, opencode:opencodeProvider, claude:claudeProvider, codex:codexProvider,
  };
  function providerFor(name: string, selectedShell: TerminalShell) {
    const inherited = selectedShell === 'cmd' && contract?.scope === scope && contract.selected === name ? contract.provider : '';
    return selectedProviders[name] ?? inherited;
  }
  const eligibleProviders = (name: string) => kimiProfiles.filter(row =>
    (name !== 'claude' || claudeProviderSupported(row)) && (name !== 'codex' || codexProviderSupported(row)));
  const [lightSgr, setLightSgr] = useState(false);
  const lightSgrRef = useRef(false);
  const outputQueue = useRef(new TerminalOutputQueue());
  const imeGate = useRef(new ImePunctuationGate());
  const draining = useRef(false);

  useEffect(() => {
    if (recoveryOpen && recoveryDialog.current) {
      const dialog = recoveryDialog.current;
      if (!dialog.open) dialog.showModal();
      dialog.querySelector<HTMLElement>("h3")?.focus();
      const resize = new ResizeObserver(() => {
        const focused = document.activeElement;
        if (focused instanceof HTMLElement && dialog.contains(focused)) {
          focused.scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      });
      resize.observe(dialog);
      return () => resize.disconnect();
    }
  }, [recoveryOpen, currentCase?.id]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    setActiveSession(null); setAttachment(null); setSessions([]); setSessionScope(scope); setPreview(null);
    requestedClose.current.clear(); dismissed.current.clear(); closeInFlight.current.clear();
    setCloseBusy(new Set()); setLoadError(""); setActionError(""); setAgentTransferError(""); setNotice("");
    return () => {
      const read = sessionRead.current;
      if (read?.scope === scope) { read.controller.abort(); sessionRead.current = null; }
    };
  }, [scope]);

  const reloadSessions = useCallback(async () => {
    if (!user) return;
    const active = sessionRead.current;
    if (active?.scope === scope) return active.done;
    active?.controller.abort();
    const controller = new AbortController();
    let finishRead!: () => void;
    const done = new Promise<void>(resolve => { finishRead = resolve; });
    const pending = { scope, controller, done };
    sessionRead.current = pending;
    const sequence = ++loadSequence.current;
    const stillCurrent = () => !controller.signal.aborted && mounted.current && currentScope.current === scope && sequence === loadSequence.current;
    try {
      // A slow reply must finish before another poll starts; replacing it every
      // 1.5 seconds would discard every valid result on a slow connection.
      const r = await sessionHistoryRequest<{ sessions: TermSession[] }>("/terminal/sessions", { signal: controller.signal });
      if (!stillCurrent()) return;
      const missing = [...requestedClose.current].filter(id => !r.sessions.some(row => row.id === id));
      const completed = await Promise.all(missing.map(id => sessionHistoryRequest<TermSession>(`/terminal/sessions/${encodeURIComponent(id)}`, { signal: controller.signal })));
      if (!stillCurrent()) return;
      const rows = [...r.sessions, ...completed].filter(row => row.case_id === (currentCase?.id ?? null));
      for (const row of rows) {
        if (requestedClose.current.has(row.id) && row.phase === "closed" && row.cleanup_verified === true) {
          requestedClose.current.delete(row.id); dismissed.current.add(row.id);
          setNotice(`终端 ${row.id.slice(0, 8)} 已关闭，停止已核验。`);
          if (activeSessionRef.current === row.id) { setActiveSession(null); setAttachment(null); }
        }
      }
      setSessions(rows.filter(row => !dismissed.current.has(row.id))); setSessionScope(scope);
      setLoadError("");
    } catch {
      if (stillCurrent()) setLoadError("会话状态读取失败，请重新读取。上次读取的状态暂时保留。");
    } finally {
      if (sessionRead.current === pending) sessionRead.current = null;
      finishRead();
    }
  }, [scope, user, currentCase?.id]);

  // CLI agent 探测（能力自检里带）
  useEffect(() => {
    if (!user) return;
    const userId = user.id;
    const sameUser = () => mounted.current && useStore.getState().user?.id === userId;
    setCliAgents({}); setProfileEnvs({});
    api.get<{ cli_agents?: Record<string, string> }>("/runtime/capabilities")
      .then((r) => { if (sameUser()) setCliAgents(r.cli_agents ?? {}); }).catch(() => {});
    api.get<Record<string, { env: Record<string, string> }>>("/runtime/cli-profiles")
      .then((profiles) => { if (sameUser()) setProfileEnvs(Object.fromEntries(
        Object.entries(profiles).map(([name, value]) => [name, value.env]))); }).catch(() => {});
  }, [user]);

  // xterm 初始化（一次）
  useEffect(() => {
    const term = new Terminal({
      fontSize: 13,
      fontFamily: "'Cascadia Code', Consolas, monospace",
      theme: { background: "#0f1115", foreground: "#d7dce5" },
      scrollback: 50000,              // 对齐桌面版（长求解日志回看）
      convertEol: false,
      cursorBlink: true,
      cursorStyle: "bar",
      cursorWidth: 2,
      drawBoldTextInBrightColors: false,
      minimumContrastRatio: 4.5,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());   // 日志里的 URL 可点击
    term.open(hostRef.current!);
    fit.fit();
    termRef.current = term;
    const selection = term.onSelectionChange(() => setHasSelection(term.hasSelection()));
    const ro = new ResizeObserver(() => fit.fit());
    ro.observe(hostRef.current!);
    let composing = false;
    const compositionStart = () => { composing = true; imeGate.current.clear(); };
    const forward = (event: InputEvent | CompositionEvent) => {
      const ws = wsRef.current;
      return ws?.readyState === WebSocket.OPEN && imeGate.current.handle(
        event instanceof InputEvent ? "beforeinput" : "compositionend", event.data ?? "", performance.now(),
        value => ws.send(JSON.stringify({ type: "input", data: value })),
      );
    };
    const beforeInput = (event: InputEvent) => {
      // xterm owns candidate editing and committed text for native compositions.
      if (!composing && !event.isComposing && event.cancelable &&
          !["insertFromPaste", "insertFromDrop"].includes(event.inputType) && forward(event)) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    };
    const compositionEnd = (event: CompositionEvent) => {
      if (composing) { composing = false; return; }
      if (forward(event)) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    };
    const textarea = hostRef.current!.querySelector(".xterm-helper-textarea") as HTMLTextAreaElement;
    textarea.dataset.vfImePatch = "1";
    textarea.addEventListener("beforeinput", beforeInput, true);
    textarea.addEventListener("compositionstart", compositionStart, true);
    textarea.addEventListener("compositionend", compositionEnd, true);
    return () => {
      ro.disconnect(); selection.dispose(); wsRef.current?.close(); term.dispose();
      textarea.removeEventListener("beforeinput", beforeInput, true);
      textarea.removeEventListener("compositionstart", compositionStart, true);
      textarea.removeEventListener("compositionend", compositionEnd, true);
    };
  }, []);

  useEffect(() => {
    lightSgrRef.current = lightSgr;
    const term = termRef.current;
    if (term) term.options.theme = lightSgr
      ? { background: "#ffffff", foreground: "#111827", cursor: "#2563eb", cursorAccent: "#eff6ff" }
      : { background: "#0f1115", foreground: "#d7dce5" };
    if (hostRef.current) hostRef.current.style.background = lightSgr ? "#ffffff" : "#0f1115";
  }, [lightSgr]);
  useEffect(() => { setLightSgr(appTheme === "light"); }, [appTheme]);

  // shell/会话切换 → 重连（挂接已有会话时补发 backlog；不杀后台 PTY）
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    wsRef.current?.close();
    wsRef.current = null;
    term.reset();
    setHasSelection(false);
    outputQueue.current.clear();
    imeGate.current.clear();
    draining.current = false;
    hostRef.current?.classList.remove("vf-output-active");
    term.options.cursorBlink = true;
    setResizeNotice("");
    if (!attachment || attachment.scope !== scope || !user) return;
    const selected = attachment;
    const sgr = new TerminalSgrStream();
    let decoder = new TextDecoder();
    let disposed = false;
    const isCurrent = () => !disposed && currentScope.current === selected.scope;
    const drainOutput = () => {
      if (draining.current) return;
      draining.current = true;
      hostRef.current?.classList.add("vf-output-active");
      term.options.cursorBlink = false;
      const writeNext = () => {
        if (!isCurrent()) return;
        const chunk = outputQueue.current.nextChunk();
        if (chunk === null) {
          draining.current = false;
          hostRef.current?.classList.remove("vf-output-active");
          term.options.cursorBlink = true;
          return;
        }
        let start = 0;
        if (chunk.truncated) {
          decoder = new TextDecoder();
          sgr.clear();
          while (start < chunk.bytes.length && (chunk.bytes[start] & 0xc0) === 0x80) start++;
        }
        const fresh = decoder.decode(chunk.bytes.subarray(start), { stream: true });
        const output = (chunk.truncated ? "\x18\x1b[0m" : "") + sgr.decode(fresh, lightSgrRef.current);
        term.write(output, writeNext);
      };
      writeNext();
    };
    const attach = selected.id ? `挂接 ${selected.id}` : `新建 ${selected.shell === "cmd" ? "Windows cmd" : selected.shell === "wsl" ? "WSL vibeflow-linux" : "WSL 隔离沙箱"}`;
    term.writeln(`\x1b[90m[${attach}…]\x1b[0m`);
    const authorization = new AbortController();
    let cleanupConnection: (() => void) | undefined;
    const connect = async () => {
      const { ticket } = await api.post<{ ticket: string }>("/connection-tickets", {
        scope: "terminal", case_id: currentCase?.id, session_id: selected.id || "",
      }, undefined, authorization.signal);
      if (!isCurrent()) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const q = selected.id
        ? `session=${encodeURIComponent(selected.id)}&token=${encodeURIComponent(ticket)}`
        : `shell=${selected.shell}&token=${encodeURIComponent(ticket)}${currentCase ? `&case=${currentCase.id}` : ""}${selected.providerId ? `&cli_harness=${encodeURIComponent(selected.harness || "")}&provider_profile_id=${selected.providerId}` : ""}`;
      const ws = new WebSocket(
        `${proto}://${location.host}/ws/terminal?cols=${term.cols}&rows=${term.rows}&${q}`
      );
      ws.binaryType = "arraybuffer";
      let resizeTimer: ReturnType<typeof setTimeout> | null = null;
      let resizeAttempts = 0;
      let lastDimensions = "";
      let commandSent = false;
      let processEnded = false;
      let connectedSessionId = selected.id;
      let ready = false;
      const requestResize = (force = false, initial = false) => {
        if (!isCurrent() || ws.readyState !== WebSocket.OPEN) return;
        const dimensions = `${term.cols}:${term.rows}`;
        if (!force && dimensions === lastDimensions) return;
        lastDimensions = dimensions;
        ws.send(JSON.stringify({ type: initial ? "ready" : "resize", cols: term.cols, rows: term.rows }));
      };
      const retryResize = () => { resizeAttempts = 0; requestResize(true); };
      retryResizeRef.current = retryResize;
      ws.onmessage = (ev) => {
        if (!isCurrent()) return;
        if (typeof ev.data === "string") {
          try {
            const m = JSON.parse(ev.data);
            if (m.type === "resize_accepted") {
              if (m.cols === term.cols && m.rows === term.rows) { resizeAttempts = 0; setResizeNotice(""); }
            } else if (m.type === "exit") {
              processEnded = true;
              term.writeln(`\r\n[进程退出 code=${m.code ?? "未知"}——会话保留可回看，关闭标签请点 ×]`);
              reloadSessions();
            } else if (m.type === "error") {
              if (m.retryable === true && (m.operation === "resize" || m.operation === "ready")) {
                if (resizeTimer !== null) clearTimeout(resizeTimer);
                resizeAttempts += 1;
                setResizeNotice(resizeAttempts <= 6 ? "窗口大小调整遇到暂时占用，正在重试。" : "窗口大小调整暂未完成，可重试调整。");
                if (resizeAttempts <= 6) resizeTimer = setTimeout(() => requestResize(true), 1500);
                return;
              }
              if (!requestedClose.current.has(connectedSessionId)) setActionError(ready
                ? `终端操作未能完成：${m.message || "请核对会话状态"}。请先核对状态；命令不会自动重发。`
                : selected.id ? `连接未能完成：${m.message || "请核对会话状态"}。可重新连接原会话。`
                : `终端未能启动：${m.message || "请检查运行环境"}。可重试新建终端。`);
              term.writeln(`\r\n[终端错误 ${m.code ?? ""}] ${m.message ?? "请查看会话记录"}`);
              reloadSessions();
            } else if (m.type === "ready") {
              connectedSessionId = m.session; ready = true;
              if (selected.command === pythonConsoleCommand) sessionStorage.setItem(pythonSessionKey(selected.scope, m.session), "1");
              setReadyAttachment(selected);
              if (!selected.id) setActiveSession(m.session);
              reloadSessions();
              // The registered terminal must be ready before sending its initial command.
              if (!selected.id && selected.command && !commandSent) {
                commandSent = true;
                ws.send(JSON.stringify({ type: "input", data: selected.command + "\r" }));
              }
            }
          } catch { /* 非 JSON 忽略 */ }
        } else {
          outputQueue.current.push(new Uint8Array(ev.data));
          drainOutput();
        }
      };
      ws.onopen = () => {
        if (!isCurrent()) return;
        requestResize(true, true);
        term.focus();
      };
      ws.onclose = () => {
        if (!isCurrent()) return;
        outputQueue.current.clear();
        draining.current = false;
        hostRef.current?.classList.remove("vf-output-active");
        term.options.cursorBlink = true;
        term.writeln("\r\n[连接已断开，会话状态待核验。沙箱文件可在“恢复文件”中查看。]");
        if (connectedSessionId && !processEnded && !requestedClose.current.has(connectedSessionId))
          setActionError(previous => previous || "连接已断开，可重新连接原会话。");
        if (!selected.id) setActiveSession(previous => previous === "" ? null : previous);
        void reloadSessions();
      };
      wsRef.current = ws;

      const sub = term.onData((d) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data: d }));
      });
      const onResize = () => requestResize();
      const ro = new ResizeObserver(onResize);
      ro.observe(hostRef.current!);
      cleanupConnection = () => {
        if (resizeTimer !== null) clearTimeout(resizeTimer);
        if (retryResizeRef.current === retryResize) retryResizeRef.current = null;
        ws.onmessage = null; ws.onopen = null; ws.onclose = null;
        ws.close();
        if (wsRef.current === ws) wsRef.current = null;
        sub.dispose(); ro.disconnect();
        outputQueue.current.clear();
        hostRef.current?.classList.remove("vf-output-active");
        term.options.cursorBlink = true;
      };
    };
    void connect().catch(error => {
      if (isCurrent()) setActionError(error instanceof Error ? error.message : "无法授权终端连接");
    });
    return () => {
      disposed = true;
      authorization.abort();
      cleanupConnection?.();
    };
  }, [attachment, scope, user, currentCase?.id, reloadSessions]);

  // 初始装载会话列表 + 轮询
  useEffect(() => {
    void reloadSessions();
    const t = setInterval(() => void reloadSessions(), 1500);
    return () => clearInterval(t);
  }, [reloadSessions]);

  // 快速输入（多行 → 逐行送进 PTY；对齐原版 TerminalQuickInputPanel 语义）
  const sendQuick = () => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN || !quickText.trim()) return;
    for (const line of quickText.split("\n")) {
      ws.send(JSON.stringify({ type: "input", data: line + "\r" }));
    }
    setQuickText("");
    termRef.current?.focus();
  };

  const terminalReady = Boolean(user && activeSess && readyAttachment === attachment);
  const canTransfer = Boolean(currentCase && terminalReady);
  const capturing = Boolean(attachment && capturingAttachment === attachment);

  const openSelectionPreview = () => {
    if (!terminalReady || !attachment) return;
    const selected = termRef.current?.getSelection();
    if (!selected?.trim()) return;
    setPreview({ origin: attachment, text: selected.slice(0, terminalPreviewLimit), truncated: selected.length > terminalPreviewLimit });
  };

  const sendToAgent = () => {
    const term = termRef.current;
    if (!term || !canTransfer) return;
    setAgentTransferError("");
    const selection = term.getSelection();
    const buffer = term.buffer.active;
    const end = Math.min(buffer.length, buffer.baseY + buffer.cursorY + 1);
    const lines: string[] = [];
    for (let index = Math.max(0, end - 20); index < end; index++) {
      lines.push(buffer.getLine(index)?.translateToString(true) ?? "");
    }
    const content = selection || lines.join("\n").trimEnd();
    if (!content.trim()) { setAgentTransferError("终端没有可加入的文本。"); return; }
    useStore.getState().updateAgentDraft(draft => ({
      ...draft, input: draft.input.trim() ? `${draft.input.trimEnd()}\n\n${content}` : content,
    }));
    activatePanel("agent");
  };

  const screenshotToAgent = async () => {
    const project = captureProjectCase();
    const host = hostRef.current;
    const source = attachment;
    if (!project || !host || !source || !canTransfer || capturing) return;
    const isCurrent = () => mounted.current && project.isCurrent() && currentAttachment.current === source;
    setAgentTransferError("");
    setCapturingAttachment(source);
    try {
      const count = currentAgentDraft(useStore.getState()).images.length;
      if (count >= maxMessageImages) throw new Error("每条消息最多 3 张图片，请先移除一张再截图。");
      const image = await captureTerminalScreenshot(host);
      if (!isCurrent()) return;
      const images = await readImageFiles([image], count);
      if (!isCurrent()) return;
      useStore.getState().updateAgentDraft(draft => {
        if (draft.images.length >= maxMessageImages) throw new Error("每条消息最多 3 张图片，请先移除一张再截图。");
        return { ...draft, images: [...draft.images, ...images] };
      });
      activatePanel("agent");
    } catch (error) {
      if (isCurrent()) setAgentTransferError(error instanceof Error ? error.message : "未能截取终端画面，请重试。");
    } finally {
      if (mounted.current) setCapturingAttachment(previous => previous === source ? null : previous);
    }
  };

  const closeSession = async (sid: string) => {
    if (closeInFlight.current.has(sid)) return;
    const origin = scope;
    closeInFlight.current.add(sid); requestedClose.current.add(sid);
    setCloseBusy(previous => new Set(previous).add(sid)); setActionError(""); setNotice("");
    try {
      await api.del(`/terminal/sessions/${encodeURIComponent(sid)}?wait=false`);
      if (mounted.current && currentScope.current === origin) await reloadSessions();
    } catch (failure) {
      if (mounted.current && currentScope.current === origin) { setActionError(closeError(failure)); await reloadSessions(); }
    } finally {
      if (mounted.current && currentScope.current === origin) {
        closeInFlight.current.delete(sid);
        setCloseBusy(previous => { const next = new Set(previous); next.delete(sid); return next; });
      }
    }
  };

  return (
    <div className="terminal-panel" data-testid="terminal-panel">
      {/* 会话标签条 + CLI agent 启动器 */}
      <div className="terminal-session-bar" role="group" aria-label="终端会话">
        <button type="button" onClick={() => setActiveSess("")} disabled={activeSess === ""}
                title="在当前算例目录新建终端">新建终端</button>
        {sessions.map((t) => (
          <div key={t.id} className="terminal-session-tab" data-session-id={t.id}>
            <button type="button" aria-pressed={activeSess === t.id} title={`${t.shell} · ${t.id}`}
              onClick={() => setActiveSess(t.id, t.shell)}>{t.shell} · {t.id.slice(0, 8)}<span>{sessionState(t)}</span></button>
            <button type="button" aria-label={`关闭终端 ${t.id.slice(0, 8)}`}
              disabled={t.finalizing || closeBusy.has(t.id) || (!t.owner_available && !t.completion_retained && t.cleanup_verified !== true)}
              onClick={() => void closeSession(t.id)}>{t.finalizing || closeBusy.has(t.id) ? "关闭中" : "关闭"}</button>
          </div>
        ))}
        <span style={{ flex: 1 }} />
        {!pythonActive && <><select data-testid="cli-profile" value={cliProfile} onChange={(event) => setCliProfile(event.target.value)}
                title="Codex WSL 兼容配置档" style={{ background: "#171a21", color: "#8b93a7", fontSize: 14 }}>
          <option value="default">default</option>
          <option value="codex-wsl">codex-wsl</option>
        </select>
        {Object.keys(cliAgents).map((name) => (
          <button key={name} style={s.agentBtn} disabled={activeSess === ""} title={`在算例目录启动 ${name}（CLI agent）`}
                  onClick={() => {
                    const nextShell = name === "codex" && cliProfile === "codex-wsl" ? "wsl" : shell;
                    try {
                      if (name === "codex" && cliProfile === "codex-wsl" && !profileEnvs[cliProfile]) {
                        throw new Error("尚未读取 Codex WSL 兼容配置，请刷新页面后重试。");
                      }
                      const provider = providerFor(name, nextShell);
                      if (provider && nextShell !== "cmd") throw new Error("API 配置启动当前支持 Windows CLI，请选择 Windows cmd 或改用 CLI 原配置。");
                      if (provider && !eligibleProviders(name).some(row => String(row.id) === provider)) {
                        throw new Error("所选 API 配置暂不可用，请重新选择。");
                      }
                      let command = commandForCliAgent(name, cliProfile, profileEnvs[cliProfile] ?? {}, cliAgents[name], nextShell);
                      if (provider && name === "opencode") command += " --standalone";
                      setActiveSess("", nextShell, command, provider ? Number(provider) : undefined, provider ? name : undefined);
                    } catch (failure) {
                      setActionError(failure instanceof Error ? failure.message : "CLI 启动配置无效，请检查运行环境配置。");
                    }
                  }}>
            {name} ▸
          </button>
        ))}
        </>}
      </div>
      {!pythonActive && (cliAgents.kimi || cliAgents.opencode || cliAgents.claude || cliAgents.codex) && <div className="terminal-provider-controls">
        {(["kimi", "opencode", "claude", "codex"] as const).filter(name => cliAgents[name]).map(name => <label className="terminal-provider-field" key={name}>
        <span>{providerLabels[name]} API 配置</span>
        <select aria-label={`${providerLabels[name]} API 配置`} value={providerFor(name, shell)} disabled={providerLoading || activeSess === ""}
          onChange={event => ({ kimi: setKimiProvider, opencode: setOpencodeProvider, claude: setClaudeProvider, codex: setCodexProvider })[name](event.target.value)}>
          <option value="">使用 CLI 原配置</option>
          {providerFor(name, shell) && !eligibleProviders(name).some(row => String(row.id) === providerFor(name, shell)) &&
            <option value={providerFor(name, shell)}>所选配置暂不可用</option>}
          {eligibleProviders(name).map(row => <option key={row.id} value={row.id}>{row.name} · {row.default_model}</option>)}
        </select>
        </label>)}
        <button type="button" disabled={providerLoading} onClick={() => void reloadProviders()}>{providerLoading ? "读取中…" : "重新读取 API 配置"}</button>
        <p>用于新建 Windows CLI 会话。Claude Code 支持 Anthropic Messages 和官方 DeepSeek；Codex 要求服务支持 Responses API。自己的配置需设置密钥和默认模型；可在 Agent 面板管理。</p>
        {providerError && <p role="alert">{providerError}</p>}
      </div>}
      {(loadError || actionError) && <div className="terminal-feedback" role="alert">
        <p>{actionError || loadError}</p><button type="button" onClick={() => void reloadSessions()}>重新读取状态</button>
        {actionError && activeSess && <button type="button" onClick={() => setActiveSess(activeSess, attachment?.shell)}>重新连接原会话</button>}
      </div>}
      {agentTransferError && <p className="terminal-feedback" role="alert">{agentTransferError}</p>}
      {notice && <p className="terminal-feedback" role="status">{notice}</p>}
      {resizeNotice && <div className="terminal-feedback" role="status"><p>{resizeNotice}</p>
        <button type="button" onClick={() => retryResizeRef.current?.()}>重试调整窗口</button></div>}
      {sessions.filter(row => row.completion_pending || row.close_error || row.finalizing).map(row => (
        <div key={row.id} className="terminal-feedback" data-testid="terminal-close-status" data-session-id={row.id}>
          <p role="status"><strong>{row.shell} · {row.id.slice(0, 8)}</strong>：{sessionState(row)}。</p>
          {!row.finalizing && <button type="button" disabled={closeBusy.has(row.id)}
            onClick={() => void closeSession(row.id)}>{closeBusy.has(row.id) ? "正在提交…" : row.completion_pending ? "重试保存关闭结果" : "重试关闭"}</button>}
        </div>
      ))}
      <div className="terminal-tools" style={s.bar}>
        <button type="button" className="terminal-recovery-toggle" ref={recoveryButton}
          aria-expanded={recoveryOpen} aria-haspopup="dialog" onClick={() => setRecoveryOpen(true)}>
          恢复文件
        </button>
        <div className="terminal-shell-choice" role="group" aria-label="新建终端类型">
          <span>新建类型</span>
          {(["cmd", "wsl", "wsl-sandbox"] as const).map((x) => (
            <button type="button" key={x} aria-pressed={shell === x}
                    onClick={() => setShellChoice({scope, shell: x})} disabled={activeSess === ""}>
              {x === "cmd" ? "Windows cmd" : x === "wsl" ? "WSL" : "隔离沙箱"}
            </button>
          ))}
        </div>
        <span className="terminal-context">
          {activeSess === "" ? "正在启动终端…" : activeSess
            ? `${pythonActive ? "Python 控制台" : attachment?.shell === "cmd" ? "Windows cmd" : attachment?.shell === "wsl" ? "WSL" : "隔离沙箱"} · ${activeSess.slice(0, 8)}`
            : shell === "cmd" ? "Windows 命令提示符" : shell === "wsl" ? "vibeflow-linux 发行版（普通 Linux 环境）" : "隔离沙箱，算例文件经暂存同步"}
        </span>
        {pythonActive && <button type="button" disabled={!terminalReady} onClick={() => {
          const ws = wsRef.current;
          if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "input", data: "\x03" }));
        }}>中断代码</button>}
        <button data-testid="terminal-light-sgr" style={{ ...s.segBtn, border: "1px solid #2a2f3a",
                         borderRadius: 5, background: lightSgr ? "#f1f5f9" : "#171a21",
                         color: lightSgr ? "#111827" : "#8fa8cc" }}
                onClick={() => setLightSgr(!lightSgr)} title="浅色主题下将黑色 ANSI 背景映射为浅灰">
          {lightSgr ? "深色 SGR" : "浅色 SGR"}
        </button>
        <button type="button" data-testid="terminal-to-agent" disabled={!canTransfer} style={{ marginLeft: "auto" }}
                onClick={sendToAgent} title="将终端选区或最近 20 行输出加入 Agent 草稿">
          输出到 Agent
        </button>
        <button type="button" data-testid="terminal-screenshot-to-agent" disabled={!canTransfer || capturing}
                onClick={() => void screenshotToAgent()} title="将当前可见终端画面加入 Agent 草稿">
          {capturing ? "正在截图…" : "截图到 Agent"}
        </button>
        <button type="button" ref={previewButton} data-testid="terminal-preview-selection"
                disabled={!terminalReady || !hasSelection} aria-haspopup="dialog" onClick={openSelectionPreview}>
          预览选区
        </button>
        <button style={{ ...s.segBtn, border: "1px solid #2a2f3a",
                         borderRadius: 5, background: quickOpen ? "#1c2740" : "#171a21",
                         color: quickOpen ? "#7db4ff" : "#8b93a7" }}
                onClick={() => setQuickOpen(!quickOpen)}
                title="多行快速输入（长命令先在这里编辑再送入终端）">
          {quickOpen ? "收起输入框 ▾" : "快速输入 ▸"}
        </button>
      </div>
      {quickOpen && (
        <div style={{ display: "flex", gap: 6, padding: 6, borderBottom: "1px solid #2a2f3a",
                      background: "#10141c" }}>
          <textarea
            style={{ flex: 1, minHeight: 56, padding: 6, background: "#0f1115", color: "#e8eaf0",
                     border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12,
                     fontFamily: "Consolas, monospace", resize: "vertical" }}
            placeholder="多行命令，每行一条；Ctrl+Enter 发送"
            value={quickText}
            onChange={(e) => setQuickText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && e.ctrlKey && !e.nativeEvent.isComposing) { e.preventDefault(); sendQuick(); }
            }}
          />
          <button style={s.sendBtn} onClick={sendQuick} disabled={!quickText.trim()}>
            发送
          </button>
        </div>
      )}
      {recoveryOpen && <dialog ref={recoveryDialog} className="terminal-recovery-dialog" aria-label="恢复沙箱文件"
        onKeyDown={event => {
          if (event.key !== "Tab") return;
          const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), select:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]',
          )).filter(element => element.getClientRects().length > 0);
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (!first) return;
          if (event.shiftKey && (!controls.includes(document.activeElement as HTMLElement) || document.activeElement === first)) {
            event.preventDefault(); last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault(); first.focus();
          }
        }}
        onClose={() => { setRecoveryOpen(false); recoveryButton.current?.focus(); }}>
        <TerminalRecovery key={currentCase?.id ?? "none"} caseId={currentCase?.id} initialSession={activeSess ?? ""}
          onClose={() => recoveryDialog.current?.close()} />
      </dialog>}
      {user && <NativeSessionHistory key={`${user.id}:${currentCase?.id ?? 'all'}`} userId={user.id} caseId={currentCase?.id}
        initialKind="terminal" onTerminalSelect={id => setActiveSess(id, sessions.find(row => row.id === id)?.shell ?? shell)} />}
      {!attachment && <div className="terminal-empty" role="status">
        <p>选择已有会话继续操作，或在当前算例目录启动终端。</p>
        <button type="button" onClick={() => setActiveSess("")}>启动 {shell === "cmd" ? "Windows cmd" : shell === "wsl" ? "WSL" : "隔离沙箱"}</button>
      </div>}
      <div ref={hostRef} className="terminal-output" style={{ display: attachment?.scope === scope ? "block" : "none", background: "#0f1115" }} />
      {preview && terminalReady && preview.origin === attachment && <TerminalSelectionPreview text={preview.text} truncated={preview.truncated}
        onClose={() => {
          setPreview(null);
          const button = previewButton.current;
          if (button && !button.disabled) button.focus();
          else termRef.current?.focus();
        }} />}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  bar: { display: "flex", flexWrap: "wrap", gap: 8, padding: "4px 8px", borderBottom: "1px solid #2a2f3a", alignItems: "center" },
  segBtn: { padding: "3px 10px", border: "none", cursor: "pointer", fontSize: 11 },
  sendBtn: { padding: "0 16px", background: "#3b82f6", color: "#fff", border: "none",
             borderRadius: 6, cursor: "pointer", fontSize: 12 },
  agentBtn: { padding: "1px 10px", background: "#1d2a1f", color: "#86efac",
              border: "1px solid #14532d", borderRadius: 4, cursor: "pointer", fontSize: 14 },
};
