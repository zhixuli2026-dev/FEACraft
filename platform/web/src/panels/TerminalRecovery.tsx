import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  recoveryDate, recoveryDownload, recoveryError, recoveryPath, recoveryPhase, recoveryReason,
  recoveryRequest, recoverySize,
} from "../utils/terminalRecovery";
import type { RecoveryDetail, RecoveryInspection, RecoveryPage, RecoveryRecord, RecoverySession } from "../utils/terminalRecovery";
import "./TerminalRecovery.css";
import TerminalRestore from "./TerminalRestore";
import TerminalRetirement from "./TerminalRetirement";

export default function TerminalRecovery({ caseId, initialSession, onClose }: {
  caseId?: number; initialSession: string; onClose: () => void;
}) {
  const selectId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const [sessions, setSessions] = useState<RecoverySession[]>([]);
  const [selected, setSelected] = useState(initialSession);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    if (!caseId) { setLoading(false); return; }
    recoveryRequest<{ sessions: RecoverySession[] }>("/terminal/sessions?include_closed=true", controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        const eligible = value.sessions.filter(session => session.shell === "wsl-sandbox" && session.case_id === caseId).reverse();
        setSessions(eligible);
        setSelected(previous => eligible.some(session => session.id === previous) ? previous : eligible[0]?.id ?? "");
      }).catch(failure => { if (!controller.signal.aborted) setError(recoveryError(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [caseId, revision]);
  return <section className="terminal-recovery" aria-labelledby={selectId + "-heading"}>
    <div className="terminal-recovery-heading">
      <h3 id={selectId + "-heading"} tabIndex={-1} ref={heading}>恢复沙箱文件</h3>
      <button type="button" onClick={onClose}>关闭恢复文件</button>
    </div>
    <p className="terminal-recovery-muted">从保留的工作副本生成 ZIP，包含文件、空目录和权限信息。查看内容后，也可预览并写回选中文件的内容。</p>
    {!caseId ? <p role="status">请先选择需要恢复文件的算例。</p> : <>
      <div className="terminal-recovery-selection">
        <label htmlFor={selectId}>沙箱会话</label>
        <select id={selectId} value={selected} disabled={loading || !sessions.length} onChange={event => setSelected(event.target.value)}>
          {!sessions.length && <option value="">{loading ? "正在读取会话…" : "暂无沙箱会话"}</option>}
          {sessions.map(session => <option key={session.id} value={session.id}>
            {session.id.slice(0, 12)} · {session.phase === "active" ? "运行中" : session.phase === "failed" ? "异常结束" : recoveryPhase(session.phase)}
          </option>)}
        </select>
        <button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>
          {loading ? "正在读取…" : "刷新会话"}
        </button>
      </div>
      {error && <p role="alert" className="terminal-recovery-error">{error}</p>}
      {!loading && !error && !sessions.length && <p role="status">当前算例没有沙箱会话记录。使用隔离沙箱产生的工作副本会显示在这里。</p>}
      {selected && sessions.some(session => session.id === selected) && <SessionRecovery key={selected} session={selected} />}
    </>}
  </section>;
}

function SessionRecovery({ session }: { session: string }) {
  const live = useRef(true);
  const listRequest = useRef<AbortController | null>(null);
  const olderPages = useRef(false);
  const inspectionRequest = useRef<AbortController | null>(null);
  const downloadRequest = useRef<AbortController | null>(null);
  const action = useRef(false);
  const [rows, setRows] = useState<RecoveryRecord[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [listError, setListError] = useState("");
  const [inspection, setInspection] = useState<RecoveryInspection | null>(null);
  const [checking, setChecking] = useState(false);
  const [inspectionError, setInspectionError] = useState("");
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [expanded, setExpanded] = useState("");
  const [downloading, setDownloading] = useState("");
  const [downloadProgress, setDownloadProgress] = useState("");

  const reload = useCallback(async (cursor?: string, background = false) => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setListBusy(true); setListError("");
    try {
      const value = await recoveryRequest<RecoveryPage>(recoveryPath(session) + (cursor ? "?before=" + encodeURIComponent(cursor) : ""), controller.signal);
      if (!live.current || controller.signal.aborted) return;
      if (cursor) olderPages.current = true;
      else if (!background) olderPages.current = false;
      setRows(previous => cursor ? [...previous, ...value.recoveries.filter(row => !previous.some(item => item.id === row.id))]
        : background && olderPages.current
          ? [...value.recoveries, ...previous.filter(row => !value.recoveries.some(item => item.id === row.id))]
          : value.recoveries);
      if (!background || !olderPages.current) setNext(value.next_before);
    } catch (error) {
      if (live.current && !controller.signal.aborted) setListError(recoveryError(error));
    } finally { if (live.current && !controller.signal.aborted) setListBusy(false); }
  }, [session]);

  const inspect = useCallback(async () => {
    inspectionRequest.current?.abort();
    const controller = new AbortController();
    inspectionRequest.current = controller;
    setChecking(true); setInspection(null); setInspectionError("");
    try {
      const value = await recoveryRequest<RecoveryInspection>("/terminal/sessions/" + encodeURIComponent(session) + "/recovery-inspection", controller.signal);
      if (live.current && !controller.signal.aborted) setInspection(value);
    } catch (error) {
      if (live.current && !controller.signal.aborted) setInspectionError(recoveryError(error));
    } finally { if (live.current && !controller.signal.aborted) setChecking(false); }
  }, [session]);

  useEffect(() => {
    live.current = true;
    void reload(); void inspect();
    return () => {
      live.current = false;
      listRequest.current?.abort(); inspectionRequest.current?.abort(); downloadRequest.current?.abort();
    };
  }, [reload, inspect]);
  const active = rows.some(row => row.phase === "active");
  useEffect(() => {
    // A just-submitted request may not yet have registered its durable row when
    // this dialog is reopened. Keep observing even when the first list is empty.
    const timer = setInterval(() => { void reload(undefined, true); }, active ? 5000 : 15000);
    return () => clearInterval(timer);
  }, [active, reload]);

  const create = async () => {
    if (action.current || checking || inspection?.status !== "processes_exited") return;
    action.current = true; setCreating(true); setActionError(""); setNotice("");
    try {
      await recoveryRequest<RecoveryDetail>(recoveryPath(session), undefined, "POST");
      if (live.current) setNotice("快照已保存，可以下载 ZIP。");
    } catch (error) {
      if (live.current) setActionError(recoveryError(error, true));
    } finally {
      action.current = false;
      if (live.current) { setCreating(false); void reload(); void inspect(); }
    }
  };

  const download = async (id: string) => {
    if (downloadRequest.current) return;
    const controller = new AbortController();
    downloadRequest.current = controller;
    setDownloading(id); setDownloadProgress("正在校验下载包…"); setActionError(""); setNotice("");
    try {
      const blob = await recoveryDownload(session, id, controller.signal, (received, total) => {
        if (live.current) setDownloadProgress("正在下载 " + recoverySize(received) + " / " + recoverySize(total));
      });
      if (!live.current || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = "sandbox-recovery-" + id + ".zip";
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      setNotice("ZIP 已交给浏览器保存，请查看下载列表。");
    } catch (error) {
      if (live.current && !controller.signal.aborted) setActionError(recoveryError(error));
    } finally {
      downloadRequest.current = null;
      if (live.current) { setDownloading(""); setDownloadProgress(""); }
    }
  };
  return <div className="terminal-recovery-session">
    <div className="terminal-recovery-condition" role="status" aria-live="polite" aria-busy={checking}>
      {checking ? "正在核验原会话是否已经停止…" : inspectionError || (inspection?.status === "processes_exited"
        ? "原会话已停止，可以创建快照。创建时会再次核验。"
        : inspection ? recoveryReason(inspection.reason) : "尚未核验恢复条件。")}
    </div>
    <div className="terminal-recovery-actions">
      <button type="button" className="terminal-recovery-primary" onClick={() => void create()}
        disabled={creating || checking || active || inspection?.status !== "processes_exited"}>
        {creating ? "正在核验并生成…" : "创建恢复快照"}
      </button>
      <button type="button" disabled={checking || creating} onClick={() => void inspect()}>重新核验</button>
      <button type="button" disabled={listBusy} onClick={() => void reload()}>{listBusy ? "正在刷新…" : "刷新记录"}</button>
    </div>
    {creating && <p role="status">正在保存恢复快照。关闭面板后，可重新打开查看记录；请勿重复创建。</p>}
    {notice && <p role="status">{notice}</p>}
    {actionError && <p role="alert" className="terminal-recovery-error">{actionError}</p>}
    {listError && <p role="alert" className="terminal-recovery-error">{listError}</p>}
    <TerminalRetirement session={session} records={rows} />
    <h4>恢复记录</h4>
    {!rows.length && !listBusy && !listError && <p className="terminal-recovery-muted">还没有恢复快照。核验通过后可创建一份。</p>}
    {listBusy && !rows.length && <p role="status">正在读取恢复记录…</p>}
    <ul className="terminal-recovery-records">
      {rows.map(row => <li key={row.id} data-recovery-id={row.id}>
        <div className="terminal-recovery-record-heading">
          <div><strong>{recoveryPhase(row.phase)}</strong><time dateTime={row.created_at}>{recoveryDate(row.created_at)}</time></div>
          <span className="terminal-recovery-id" title={row.id}>快照 {row.id.slice(0, 12)}</span>
        </div>
        {row.phase === "failed" && <p>这次生成未完成，原沙箱副本已保留。可重新核验后再尝试。</p>}
        {row.phase === "recovery_required" && <p>上次执行已中断，结果尚未确认。请先刷新记录，再重新核验。</p>}
        {row.phase === "active" && <p role="status">正在生成，记录会自动刷新。</p>}
        {row.phase === "succeeded" && row.cleanup_verified === true && <div className="terminal-recovery-actions">
          <button type="button" disabled={!!downloading} onClick={() => void download(row.id)}>
            {downloading === row.id ? "正在下载…" : "下载 ZIP"}
          </button>
          <button type="button" aria-expanded={expanded === row.id} onClick={() => setExpanded(expanded === row.id ? "" : row.id)}>
            {expanded === row.id ? "收起内容" : "查看内容"}
          </button>
        </div>}
        {downloading === row.id && <p role="status">{downloadProgress}</p>}
        {expanded === row.id && <RecoveryContents session={session} id={row.id} />}
      </li>)}
    </ul>
    {next && <button type="button" disabled={listBusy} onClick={() => void reload(next)}>加载更早记录</button>}
  </div>;
}

function RecoveryContents({ session, id }: { session: string; id: string }) {
  const [value, setValue] = useState<RecoveryDetail | null>(null);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(""); setValue(null); setPage(0);
    recoveryRequest<RecoveryDetail>(recoveryPath(session) + "/" + encodeURIComponent(id), controller.signal)
      .then(result => {
        if (!result.manifest || result.phase !== "succeeded") throw new Error("Snapshot unavailable");
        if (!controller.signal.aborted) setValue(result);
      }).catch(failure => { if (!controller.signal.aborted) setError(recoveryError(failure)); });
    return () => controller.abort();
  }, [session, id, revision]);
  const manifest = value?.manifest;
  if (error) return <div><p role="alert" className="terminal-recovery-error">{error}</p><button type="button" onClick={() => setRevision(v => v + 1)}>重试读取内容</button></div>;
  if (!manifest) return <p role="status">正在读取快照内容…</p>;
  const entries = [
    ...manifest.directories.map(path => ({ path, detail: "目录" })),
    ...Object.entries(manifest.files).map(([path, file]) => ({
      path, detail: recoverySize(file.size) + " · 权限 " + (manifest.modes[path] ?? 0).toString(8).padStart(3, "0"),
    })),
  ];
  const pages = Math.max(1, Math.ceil(entries.length / 20));
  return <div className="terminal-recovery-contents">
    <p>{Object.keys(manifest.files).length} 个文件 · {manifest.directories.length} 个目录 · {recoverySize(manifest.bytes)}</p>
    <ul aria-label="快照内容">
      {entries.slice(page * 20, (page + 1) * 20).map(entry => <li key={entry.path}><span>{entry.path}</span><span>{entry.detail}</span></li>)}
    </ul>
    {!entries.length && <p>此快照没有可显示的文件或目录。</p>}
    {pages > 1 && <div className="terminal-recovery-actions">
      <button type="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>上一页</button>
      <span role="status">第 {page + 1} / {pages} 页</span>
      <button type="button" disabled={page + 1 === pages} onClick={() => setPage(p => p + 1)}>下一页</button>
    </div>}
    <TerminalRestore session={session} id={id} files={manifest.files} />
  </div>;
}
