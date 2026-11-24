import { DockviewReact } from "dockview";
import type { DockviewApi, DockviewReadyEvent, IDockviewPanelProps } from "dockview";
import "dockview/dist/styles/dockview.css";
import { useEffect, useState } from "react";
import CasePanel from "../panels/CasePanel";
import ChatPanel from "../panels/ChatPanel";
import EditorPanel from "../panels/EditorPanel";
import GeoPanel from "../panels/GeoPanel";
import FormulaPanel from "../panels/FormulaPanel";
import FloThermPanel from "../panels/FloThermPanel";
import SettingsPanel from "../panels/SettingsPanel";
import ProviderProfilesPanel from "../panels/ProviderProfilesPanel";
import ScnetPanel from "../panels/ScnetPanel";
import ViewerPanel, { TexoPanel } from "../panels/ViewerPanel";
import VizPanel from "../panels/VizPanel";
import { activatePanel, setCompactLayout, setDockApi, workbenchPanels } from "./dock";
import "./Shell.css";
import JobsPanel from "../panels/JobsPanel";
import TerminalPanel from "../panels/TerminalPanel";
import TreePanel from "../panels/TreePanel";
import { chatgptOutcome, loginMessage } from "../auth";
import { useStore } from '../store';
import { readDerivativeSelection, readDerivativeView } from '../utils/viewSelection';

const components = {
  tree: (_p: IDockviewPanelProps) => <TreePanel />,
  editor: (_p: IDockviewPanelProps) => <EditorPanel />,
  caseinfo: (_p: IDockviewPanelProps) => <CasePanel />,
  terminal: (_p: IDockviewPanelProps) => <TerminalPanel />,
  agent: (_p: IDockviewPanelProps) => <ChatPanel />,
  jobs: (_p: IDockviewPanelProps) => <JobsPanel />,
  geometry: (_p: IDockviewPanelProps) => <GeoPanel />,
  flotherm: (_p: IDockviewPanelProps) => <FloThermPanel />,
  viewer: (_p: IDockviewPanelProps) => <ViewerPanel />,
  viz: (_p: IDockviewPanelProps) => <VizPanel />,
  texo: (_p: IDockviewPanelProps) => <TexoPanel />,
  formula: (_p: IDockviewPanelProps) => <FormulaPanel />,
  settings: (_p: IDockviewPanelProps) => <SettingsPanel />,
  providers: (_p: IDockviewPanelProps) => <ProviderProfilesPanel />,
  scnet: (_p: IDockviewPanelProps) => <ScnetPanel />,
};

export default function Shell() {
  const userId = useStore(state => state.user?.id);
  const caseId = useStore(state => state.currentCase?.id);
  const caseName = useStore(state => state.currentCase?.name);
  const activeReading = useStore(state => state.activePublication);
  const [dock, setDock] = useState<DockviewApi | null>(null);
  const [compact, setCompact] = useState(() => matchMedia('(max-width: 760px)').matches);
  const [activePanel, setActivePanel] = useState('tree');
  const [agentOpen, setAgentOpen] = useState(true);
  useEffect(() => {
    const query = matchMedia('(max-width: 760px)');
    const changed = () => setCompact(query.matches);
    query.addEventListener('change', changed);
    return () => { query.removeEventListener('change', changed); setDockApi(null); };
  }, []);
  useEffect(() => {
    if (!dock) return;
    setCompactLayout(compact);
    const subscription = dock.onDidActivePanelChange(panel => {
      if (!panel) return;
      setActivePanel(panel.id);
      if (compact) dock.maximizeGroup(panel);
    });
    if (dock.activePanel) setActivePanel(dock.activePanel.id);
    return () => subscription.dispose();
  }, [dock, compact]);
  useEffect(() => {
    if (!dock) return;
    const added = dock.onDidAddPanel(panel => { if (panel.id === 'agent') setAgentOpen(true); });
    const removed = dock.onDidRemovePanel(panel => { if (panel.id === 'agent') setAgentOpen(false); });
    return () => { added.dispose(); removed.dispose(); };
  }, [dock]);
  useEffect(() => {
    if (activeReading) activatePanel('viewer');
  }, [userId, activeReading?.caseId, activeReading?.path]);
  useEffect(() => {
    if (!userId || !caseId) return;
    const archive = readDerivativeView(userId, caseId);
    const selected = readDerivativeSelection(userId, caseId, archive.family);
    if (archive.open && (selected.id || selected.invalid)) activatePanel('caseinfo');
  }, [userId, caseId]);
  const [showAbout, setShowAbout] = useState(false);
  const [authNotice, setAuthNotice] = useState(chatgptOutcome ? loginMessage(chatgptOutcome) : "");
  const [availableRelease, setAvailableRelease] = useState<string | null>(null);
  useEffect(() => {
    const loaded = document.querySelector<HTMLMetaElement>('meta[name="vibeflow-client-release"]')?.content;
    if (!loaded) return;
    const controller = new AbortController();
    let checking = false;
    async function check() {
      if (checking || document.visibilityState !== 'visible') return;
      checking = true;
      try {
        const response = await fetch('/vibeflow-version.json', { cache:'no-store', signal:controller.signal });
        if (!response.ok) return;
        const info = await response.json();
        if (typeof info.release === 'string' && typeof info.version === 'string'
          && /^[0-9]+\.[0-9]+\.[0-9]+(?:-beta\.[0-9]+)?$/.test(info.version) && info.version.length <= 40
          && !controller.signal.aborted) setAvailableRelease(info.release !== loaded ? info.version : null);
      } catch {
        // The current client can continue while its version feed is unavailable.
      } finally {
        checking = false;
      }
    }
    void check();
    const timer = window.setInterval(check, 45_000);
    window.addEventListener('focus', check);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener('focus', check);
    };
  }, []);
  const onReady = (e: DockviewReadyEvent) => {
    setDockApi(e.api);
    // 先中间占满，再左/右/下分栏（dockview 首块面板必占满全布局）
    e.api.addPanel({ id: "editor", component: "editor", title: "编辑器" });
    e.api.addPanel({
      id: "tree", component: "tree", title: "项目",
      position: { direction: "left" }, initialWidth: 280,
    });
    e.api.addPanel({
      id: "caseinfo", component: "caseinfo", title: "caseDict",
      position: { direction: "right" }, initialWidth: 340,
    });
    // Agent 与 caseDict 同组（右侧栏内标签切换）
    e.api.addPanel({
      id: "agent", component: "agent", title: "Agent",
      position: { referencePanel: "caseinfo" },
    });
    e.api.addPanel({
      id: "terminal", component: "terminal", title: "终端",
      position: { direction: "below" }, initialHeight: 400,
    });
    // 作业与终端同组（底部标签切换）
    e.api.addPanel({
      id: "jobs", component: "jobs", title: "作业",
      position: { referencePanel: "terminal" },
    });
    // 几何预览/查看器与编辑器同组（中间标签切换）
    e.api.addPanel({
      id: "geometry", component: "geometry", title: "几何预览",
      position: { referencePanel: "editor" },
    });
    e.api.addPanel({
      id: "flotherm", component: "flotherm", title: "FloTHERM",
      position: { referencePanel: "editor" },
    });
    e.api.addPanel({
      id: "viewer", component: "viewer", title: "查看器",
      position: { referencePanel: "editor" },
    });
    // 可视化（trame 远程渲染）与编辑器同组
    e.api.addPanel({
      id: "viz", component: "viz", title: "可视化",
      position: { referencePanel: "editor" },
    });
    // texo 公式识别放右栏组
    e.api.addPanel({
      id: "texo", component: "texo", title: "公式识别",
      position: { referencePanel: "caseinfo" },
    });
    // 公式调色板与 texo 同组
    e.api.addPanel({
      id: "formula", component: "formula", title: "公式调色板",
      position: { referencePanel: "caseinfo" },
    });
    e.api.addPanel({
      id: "scnet", component: "scnet", title: "SCNET",
      position: { referencePanel: "caseinfo" },
    });
    // 设置页放右栏组末尾
    e.api.addPanel({
      id: "settings", component: "settings", title: "设置",
      position: { referencePanel: "caseinfo" },
    });
    const { activePublication: reading, user, currentCase } = useStore.getState();
    e.api.getPanel(reading ? 'viewer' : 'editor')?.api.setActive();
    if (user && currentCase) {
      const archive = readDerivativeView(user.id, currentCase.id);
      const selected = readDerivativeSelection(user.id, currentCase.id, archive.family);
      if (archive.open && (selected.id || selected.invalid)) e.api.getPanel('caseinfo')?.api.setActive();
    }
    setDock(e.api);
    if (compact) {
      setCompactLayout(true);
      activatePanel(reading ? 'viewer' : currentCase ? 'caseinfo' : 'tree');
    }
  };

  return (
    <div className="workbench-shell">
      <header style={s.topbar}>
        <span style={{ color: "#fff", fontWeight: 600 }}>🌊 VibeFlow Platform</span>
        <span style={{ color: "#667", fontSize: 12 }}>Web 版 · 开发预览</span>
        {!compact && !agentOpen && <button type="button" className="reopen-agent"
          onClick={() => activatePanel('agent')}>打开 Agent</button>}
        <button style={s.aboutBtn} onClick={() => setShowAbout(true)} title="关于与第三方声明">ⓘ</button>
      </header>
      <nav className="workbench-mobile-nav" aria-label="工作台面板">
        <label>面板<select aria-label="工作面板" value={activePanel} onChange={event => activatePanel(event.target.value)}>
          {workbenchPanels.map(panel => <option key={panel.id} value={panel.id}>{panel.title}</option>)}
        </select></label>
        <button type="button" onClick={() => activatePanel('tree')}>选择算例</button>
        <p title={caseName}>{caseName || '尚未选择算例'}</p>
      </nav>
      {availableRelease && <div className="client-version-notice" data-testid="client-version-notice">
        <span role="status">服务器已切换到 {availableRelease}</span>
        <button type="button" onClick={() => window.location.reload()}>刷新工作台</button>
      </div>}
      {authNotice && <div className="auth-surface auth-notice" role="status">
        <span>{authNotice}</span><button type="button" className="auth-link" onClick={() => setAuthNotice("")}>关闭提示</button>
      </div>}
      <div style={{ flex: 1, minHeight: 0 }}>
        <DockviewReact className="dockview-theme-dark" components={components} onReady={onReady} />
      </div>
      {showAbout && (
        <div style={s.overlay} onClick={() => setShowAbout(false)}>
          <div style={s.aboutCard} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 8px", color: "#fff" }}>🌊 VibeFlow Platform（Web 版）</h3>
            <p style={s.p}>CAE 数值仿真工作台的 B/S 形态重建（开发预览）。版本 0.1.0。</p>
            <p style={s.p}><b>文档</b>：platform/docs/（HANDOVER 交接 · progress 进度 · mechanism-audit 机制审计 · parity-checklist 对等记分卡 · api 清单）</p>
            <p style={s.p}><b>第三方资产与许可</b>（逐项核验中，以 docs/third-party.md 为准）：</p>
            <ul style={s.ul}>
              <li>npm / Python / 原生依赖：按实际发布 SBOM 与许可证清单核查</li>
              <li>PDF.js 2.16.105（Apache-2.0）及 core-js / CMap / 字体</li>
              <li>Transformers.js 2.17.2（Apache-2.0）/ ONNX Runtime Web 1.14.0（MIT）</li>
              <li>Texo / Texo-web 上游声明 AGPL-3.0；FormulaNet 权重及训练上游的再分发条件尚未闭环</li>
              <li>OpenFOAM / VTK / CadQuery / pandoc 等按最终镜像逐项核查</li>
            </ul>
            <p style={{ ...s.p, color: "#8b93a7", fontSize: 12 }}>
              平台业务代码为重写实现；仍包含上述第三方静态资产，来源与许可以核查清单为准。
            </p>
            <button style={s.closeBtn} onClick={() => setShowAbout(false)}>关闭</button>
          </div>
        </div>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  topbar: {
    display: "flex", alignItems: "center", gap: 10, padding: "6px 12px",
    background: "#171a21", borderBottom: "1px solid #2a2f3a",
  },
  aboutBtn: { marginLeft: "auto", background: "transparent", border: "1px solid #2a2f3a",
              borderRadius: 5, color: "#8b93a7", cursor: "pointer", fontSize: 12,
              padding: "1px 8px" },
  overlay: { position: "fixed", inset: 0, background: "#000a", zIndex: 100,
             display: "flex", alignItems: "center", justifyContent: "center" },
  aboutCard: { width: 560, maxHeight: "80vh", overflow: "auto", background: "#14171d",
               border: "1px solid #2a2f3a", borderRadius: 12, padding: 20,
               boxShadow: "0 12px 40px #000c" },
  p: { color: "#cfd6e4", fontSize: 13, lineHeight: 1.7, margin: "6px 0" },
  ul: { color: "#9ab3d0", fontSize: 12, lineHeight: 1.8, margin: "4px 0 4px 18px", padding: 0 },
  closeBtn: { marginTop: 10, padding: "5px 16px", background: "#2563eb", color: "#fff",
              border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
};
