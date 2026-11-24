import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { recoveryError, recoveryPath, recoveryRequest, recoverySize } from "../utils/terminalRecovery";

type FileRecord = { size?: number; sha256?: string; kind?: "file" | "directory"; mode?: number };
type Semantics = "file_contents_only" | "sandbox_file_changes" | "case_dictionary" | "sandbox_tree_changes";
type Plan = { plan_sha256: string; dictionary_revision?: number; workflow_state?: string; files: { path: string; action: string; current: FileRecord | null; snapshot: FileRecord | null }[] };
type Confirmation = { paths: string[]; plan_sha256: string; request_id: string; confirm: true; semantics: Semantics };
type Receipt = { request_id: string; written_files: string[]; unchanged_files: string[]; deleted_files?: string[]; semantics?: Semantics; replayed?: boolean; dictionary_revision?: number };
type Changes = { max_selection: number; files: { path: string; initial: FileRecord | null; snapshot: FileRecord | null; protected: boolean }[]; directory_changes: string[]; permission_changes: string[] };

function retained(key: string): Confirmation | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null") as Confirmation | null;
    return value && value.confirm === true && ["file_contents_only", "sandbox_file_changes", "case_dictionary", "sandbox_tree_changes"].includes(value.semantics)
      && Array.isArray(value.paths) && value.paths.length > 0 && value.paths.length <= (["sandbox_file_changes", "sandbox_tree_changes"].includes(value.semantics) ? 10000 : 256)
      && value.paths.every(path => typeof path === "string") && /^[a-f0-9]{64}$/.test(value.plan_sha256)
      && /^[a-f0-9-]{36}$/.test(value.request_id) ? value : null;
  } catch { return null; }
}

export default function TerminalRestore({ session, id, files }: { session: string; id: string; files: Record<string, FileRecord> }) {
  const endpoint = recoveryPath(session) + "/" + encodeURIComponent(id);
  const storageKey = "vf_recovery_confirmation:" + session + ":" + id;
  const [pending, setPending] = useState<Confirmation | null>(() => retained(storageKey));
  const [chosen, setChosen] = useState<string[]>(() => retained(storageKey)?.paths ?? []);
  const [semantics, setSemantics] = useState<Semantics>(() => retained(storageKey)?.semantics ?? "file_contents_only");
  const [changes, setChanges] = useState<Changes | null>(null);
  const [changesLoading, setChangesLoading] = useState(false);
  const [changesError, setChangesError] = useState("");
  const [planPage, setPlanPage] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const live = useRef(true);
  const action = useRef(false);
  const previewRequest = useRef<AbortController | null>(null);
  const restored = (result: Receipt) => {
    // Keep the last confirmation ID across reopening so its receipt can be read.
    if (live.current) { setReceipt(result); setPending(null); setPlan(null); setAcknowledged(false); setError(""); }
  };
  useEffect(() => {
    live.current = true;
    const controller = new AbortController();
    const previous = retained(storageKey);
    if (previous) {
      recoveryRequest<Receipt>(endpoint + "/restores/" + previous.request_id, controller.signal)
        .then(value => { if (!controller.signal.aborted) restored(value); })
        .catch(failure => {
          if (!controller.signal.aborted) setError(failure instanceof ApiError && failure.status === 404
            ? "上次写回还没有可查询的完成回执。可核对结果，或重试同一次确认。" : recoveryError(failure));
        });
    }
    return () => { live.current = false; controller.abort(); previewRequest.current?.abort(); };
  }, [endpoint, storageKey]);

  useEffect(() => {
    if (!["sandbox_file_changes", "sandbox_tree_changes"].includes(semantics)) return;
    const controller = new AbortController();
    setChangesLoading(true); setChangesError(""); setChanges(null);
    recoveryRequest<Changes>(endpoint + (semantics === "sandbox_tree_changes" ? "/tree-changes" : "/changes"), controller.signal)
      .then(value => { if (!controller.signal.aborted) setChanges(value); })
      .catch(failure => { if (!controller.signal.aborted) setChangesError(failure instanceof ApiError && failure.code === "recovery_baseline_missing"
        ? "此旧快照没有登记的初始清单。请切换到文件内容写回，逐项核对目标。" : recoveryError(failure)); })
      .finally(() => { if (!controller.signal.aborted) setChangesLoading(false); });
    return () => controller.abort();
  }, [endpoint, semantics]);

  const pendingId = pending?.request_id;
  useEffect(() => {
    if (!pendingId) return;
    const controller = new AbortController();
    let checking = false;
    const timer = setInterval(() => {
      if (checking) return;
      checking = true;
      recoveryRequest<Receipt>(endpoint + "/restores/" + pendingId, controller.signal)
        .then(value => { if (!controller.signal.aborted) restored(value); })
        .catch(() => { /* An absent receipt is not permission to resubmit. */ })
        .finally(() => { checking = false; });
    }, 5000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [endpoint, pendingId]);

  const preview = async () => {
    if (action.current || pending || !chosen.length) return;
    action.current = true; setBusy("preview"); setError(""); setPlan(null); setAcknowledged(false); setReceipt(null);
    const controller = new AbortController(); previewRequest.current = controller;
    try {
      const result = await recoveryRequest<Plan>(endpoint + "/restore-plan", controller.signal, "POST", { paths: chosen, semantics });
      if (live.current && !controller.signal.aborted) { setPlan(result); setPlanPage(0); }
    } catch (failure) { if (live.current && !controller.signal.aborted) setError(recoveryError(failure)); }
    finally { action.current = false; if (live.current) setBusy(""); }
  };
  const confirm = async (body: Confirmation) => {
    if (action.current) return;
    action.current = true; setBusy("confirm"); setError("");
    let sent = false;
    try {
      // Persist before sending: closing this view must not lose an uncertain request.
      localStorage.setItem(storageKey, JSON.stringify(body));
      setPending(body);
      try {
        sent = true;
        restored(await recoveryRequest<Receipt>(endpoint + "/restore", undefined, "POST", body));
      } catch (failure) {
        try {
          restored(await recoveryRequest<Receipt>(endpoint + "/restores/" + body.request_id));
        } catch {
          const rejected = failure instanceof ApiError && (failure.status === 422 || failure.status === 401
            || failure.status === 403 || ["recovery_plan_stale", "recovery_protected_path", "recovery_destination_type",
              "recovery_evidence_invalid", "case_source_changed", "recovery_merge_conflict", "recovery_baseline_missing",
              "recovery_dictionary_selection", "recovery_dictionary_invalid"].includes(failure.code ?? ""));
          if (rejected) localStorage.removeItem(storageKey);
          if (live.current) {
            if (rejected) { setPending(null); setPlan(null); setAcknowledged(false); }
            setError(rejected ? recoveryError(failure) : "写回结果尚未确认。请核对结果或重试同一次确认，避免创建另一笔操作。");
          }
        }
      }
    } catch {
      if (live.current) setError(sent
        ? "写回请求已发出，但浏览器无法保存后续状态。请先核对结果，避免创建另一笔操作。"
        : "无法保存本次确认标识，尚未发送写回请求。请检查浏览器存储设置。");
    } finally { action.current = false; if (live.current) setBusy(""); }
  };
  const check = async () => {
    if (!pending || action.current) return;
    action.current = true; setBusy("status"); setError("");
    try { restored(await recoveryRequest<Receipt>(endpoint + "/restores/" + pending.request_id)); }
    catch (failure) { if (live.current) setError(failure instanceof ApiError && failure.status === 404
      ? "还没有完成回执。请稍后核对，或重试同一次确认。" : recoveryError(failure)); }
    finally { action.current = false; if (live.current) setBusy(""); }
  };
  const tree = semantics === "sandbox_tree_changes";
  const merging = semantics === "sandbox_file_changes" || tree;
  const dictionary = semantics === "case_dictionary";
  const maximum = dictionary ? 1 : merging ? changes?.max_selection ?? 10000 : 256;
  const entries = new Map(changes?.files.map(item => [item.path, item]) ?? []);
  const names = (merging ? Array.from(entries.keys()) : Object.keys(files)).filter(path => (!dictionary || path === "0-caseDict/caseDict") && path.toLowerCase().includes(filter.toLowerCase())).sort();
  const protectedPath = (path: string) => !dictionary && (merging ? entries.get(path)?.protected : path.toLowerCase() === "0-casedict/casedict");
  const selectable = names.filter(path => !protectedPath(path));
  const conflicts = plan?.files.filter(file => file.action === "conflict").map(file => file.path) ?? [];
  const pages = Math.max(1, Math.ceil(names.length / 20));
  return <section className="terminal-restore" aria-label="恢复项目文件">
    <h4>恢复项目文件</h4>
    <label>恢复方式<select value={semantics} disabled={!!busy || !!pending} onChange={event => {
      setSemantics(event.target.value as Semantics); setChosen([]); setPlan(null); setPage(0); setFilter(""); setAcknowledged(false); setError(""); setReceipt(null);
    }}><option value="file_contents_only">选中文件内容写回</option><option value="sandbox_file_changes">比较并恢复沙箱文件改动</option><option value="sandbox_tree_changes">恢复目录、文件和 Linux 权限</option><option value="case_dictionary">恢复项目配置 caseDict</option></select></label>
    <p>{tree ? "比较初始副本、沙箱快照和当前项目。恢复目录、文件类型和 Linux 权限；选择目录会包含其中的沙箱改动，保留项目中新增加的内容。Windows 和 S3 保存的权限会在下一次 Linux 沙箱中应用。配置请使用专用恢复方式。" : dictionary ? "比较初始配置、沙箱快照和当前配置。通过格式、版本和阶段校验后保存，并记录新版本；双方均修改时保留冲突。" : merging ? "比较初始副本、沙箱快照和当前项目。可恢复新增、修改和删除；双方都改过的文件保留为冲突，不自动覆盖。" : "先预览当前项目中的目标文件，再确认覆盖。此方式只写回选中文件的内容，不恢复删除。"} 完整快照可下载为 ZIP。</p>
    {changesLoading && <p role="status">正在读取沙箱改动…</p>}
    {merging && changesError && <p role="alert" className="terminal-recovery-error">{changesError}</p>}
    {merging && changes && <p>{changes.files.length} 项改动；{tree ? "包含" : "另有"} {changes.directory_changes.length} 项目录变化、{changes.permission_changes.length} 项权限变化{tree ? "。" : "，本次文件写回不会处理。"}</p>}
    <label>筛选恢复文件<input type="search" value={filter} disabled={!!busy || !!pending} onChange={event => { setFilter(event.target.value); setPage(0); }} /></label>
    <p>已选择 {chosen.length} / {maximum} 个{tree ? "条目" : "文件"}</p>
    {dictionary && !names.length && <p>快照中没有可恢复的 caseDict。可下载快照，并在项目配置编辑器核对。</p>}
    {merging && <div className="terminal-recovery-actions">
      <button type="button" disabled={!!busy || !!pending || changesLoading || !selectable.length || selectable.length > maximum} onClick={() => { setChosen(selectable); setPlan(null); setAcknowledged(false); }}>选择筛选出的改动</button>
      <button type="button" disabled={!!busy || !!pending || !chosen.length} onClick={() => { setChosen([]); setPlan(null); setAcknowledged(false); }}>清空选择</button>
    </div>}
    <ul aria-label="选择写回文件">
      {names.slice(page * 20, page * 20 + 20).map(path => <li key={path}>
        <label><input type="checkbox" checked={chosen.includes(path)}
          disabled={!!busy || !!pending || protectedPath(path) || (!chosen.includes(path) && chosen.length >= maximum)}
          onChange={event => { setChosen(previous => event.target.checked ? [...previous, path] : previous.filter(name => name !== path)); setPlan(null); setAcknowledged(false); }} />
          <span>{path}</span><span>{merging && !entries.get(path)?.snapshot ? "已在沙箱删除" : tree && entries.get(path)?.snapshot?.kind === "directory" ? "目录" : recoverySize((merging ? entries.get(path)?.snapshot : files[path])?.size ?? 0)}{tree && entries.get(path)?.snapshot?.mode !== undefined ? " · " + entries.get(path)!.snapshot!.mode!.toString(8) : ""}</span></label>
      </li>)}
    </ul>
    {pages > 1 && <div className="terminal-recovery-actions">
      <button type="button" disabled={page === 0} onClick={() => setPage(value => value - 1)}>上一页文件</button>
      <span>第 {page + 1} / {pages} 页</span>
      <button type="button" disabled={page + 1 === pages} onClick={() => setPage(value => value + 1)}>下一页文件</button>
    </div>}
    <button type="button" disabled={!!busy || !!pending || !chosen.length} onClick={() => void preview()}>{busy === "preview" ? "正在预览…" : "预览写回影响"}</button>
    {plan && !pending && <div className="terminal-restore-plan">
      {plan.dictionary_revision !== undefined && <p>当前配置版本 {plan.dictionary_revision}，阶段 {plan.workflow_state}。确认时将再次核对版本，恢复不会改变当前阶段。</p>}
      <ul aria-label="写回影响">{plan.files.slice(planPage * 20, planPage * 20 + 20).map(file => <li key={file.path}><span>{file.path}</span><strong>
        {file.action === "replace_type" ? "文件与目录互换" : file.action === "create" ? (tree ? "新增条目" : "新增文件") : file.action === "already_matches" ? "内容相同，不重复写入" : file.action === "delete" ? (tree ? "删除当前条目" : "删除当前文件") : file.action === "keep_current" ? "沙箱未修改，保留当前内容" : file.action === "conflict" ? "存在冲突，保留当前内容" : tree ? "更新内容或 Linux 权限" : "更新当前文件内容"}
      </strong></li>)}</ul>
      {plan.files.length > 20 && <div className="terminal-recovery-actions">
        <button type="button" disabled={planPage === 0} onClick={() => setPlanPage(value => value - 1)}>上一页影响</button>
        <span>影响 {planPage + 1} / {Math.ceil(plan.files.length / 20)} 页</span>
        <button type="button" disabled={(planPage + 1) * 20 >= plan.files.length} onClick={() => setPlanPage(value => value + 1)}>下一页影响</button>
      </div>}
      {!!conflicts.length && <div role="alert"><p>{conflicts.length} 个条目存在冲突，不能提交。可取消选择这些条目，先恢复其余改动。{tree && "相关父目录的递归选择也会一并取消。"}</p>
        <button type="button" disabled={!!busy} onClick={() => { setChosen(previous => previous.filter(path => !conflicts.some(conflict => conflict === path || tree && conflict.startsWith(path + "/")))); setPlan(null); setAcknowledged(false); }}>{tree ? "取消选择冲突条目" : "取消选择冲突文件"}</button></div>}
      <label><input type="checkbox" checked={acknowledged} disabled={!!busy || !!conflicts.length} onChange={event => setAcknowledged(event.target.checked)} />{tree ? "我已核对目录、类型、权限和删除，确认恢复所选改动。" : dictionary ? "我已核对配置版本和影响，确认恢复 caseDict。" : merging ? "我已核对新增、修改和删除，确认恢复所选文件改动。" : "我已核对目标，确认只写回以上文件内容。"}</label>
      <button type="button" disabled={!!busy || !acknowledged || !!conflicts.length} onClick={() => void confirm({paths:chosen, plan_sha256:plan.plan_sha256,
        request_id:crypto.randomUUID(), confirm:true, semantics})}>{tree ? "确认恢复目录与文件" : dictionary ? "确认恢复项目配置" : merging ? "确认恢复文件改动" : "确认写回文件内容"}</button>
    </div>}
    {pending && <div role="status"><p>{busy === "confirm" ? "正在写回，可关闭后重新查看结果。" : "这次确认的结果尚未核实。"}</p>
      <button type="button" disabled={!!busy} onClick={() => void check()}>核对写回结果</button>
      <button type="button" disabled={!!busy} onClick={() => void confirm(pending)}>重试同一次确认</button>
    </div>}
    {receipt && <p role="status">已更新 {receipt.written_files.length - (receipt.deleted_files?.length ?? 0)} 项，删除 {receipt.deleted_files?.length ?? 0} 项，保留 {receipt.unchanged_files.length} 项。{receipt.semantics === "sandbox_tree_changes" ? "目录与 Linux 权限信息已恢复；下一次沙箱会使用这些权限。" : "目录和权限未在本次操作中恢复。"}</p>}
    {receipt?.dictionary_revision !== undefined && <p role="status">配置版本 {receipt.dictionary_revision}；版本记录与配置已一同保存。</p>}
    {error && <p role="alert" className="terminal-recovery-error">{error}</p>}
  </section>;
}
