import { useCallback, useEffect, useId, useRef, useState } from "react";
import { recoveryDate, recoveryError, recoveryRequest } from "../utils/terminalRecovery";
import type { RecoveryRecord } from "../utils/terminalRecovery";
import TerminalStagingCleanup from "./TerminalStagingCleanup";
import TerminalImageCleanup from "./TerminalImageCleanup";

type Result = { status: "not_retired" | "retired"; recovery_id?: string; observed_at?: string;
  staging_retained?: boolean; original_exit_code?: number | null };

function pendingSnapshot(key: string) {
  try { const value = localStorage.getItem(key); return value && /^[a-f0-9-]{36}$/.test(value) ? value : ""; }
  catch { return ""; }
}

export default function TerminalRetirement({ session, records }: { session: string; records: RecoveryRecord[] }) {
  const endpoint = "/terminal/sessions/" + encodeURIComponent(session) + "/retirement";
  const storageKey = "vf_terminal_retirement:" + session;
  const selectId = useId();
  const [pending, setPending] = useState(() => pendingSnapshot(storageKey));
  const [selected, setSelected] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  const action = useRef(false);
  const eligible = records.filter(row => row.phase === "succeeded" && row.cleanup_verified === true);
  const chosen = pending || (eligible.some(row => row.id === selected) ? selected : eligible[0]?.id ?? "");
  const accept = useCallback((value: Result) => {
    if (!live.current) return;
    setResult(value);
    if (value.status === "retired") {
      setPending(""); setError("");
      try { localStorage.removeItem(storageKey); } catch { /* Server receipt is authoritative. */ }
    }
  }, [storageKey]);
  const check = useCallback(async (signal?: AbortSignal) => {
    try {
      const value = await recoveryRequest<Result>(endpoint, signal);
      if (!signal?.aborted) accept(value);
      return value;
    } finally { if (live.current && !signal?.aborted) setChecking(false); }
  }, [endpoint, accept]);
  useEffect(() => {
    live.current = true;
    const controller = new AbortController();
    void check(controller.signal).catch(failure => { if (!controller.signal.aborted) setError(recoveryError(failure)); });
    return () => { live.current = false; controller.abort(); };
  }, [check]);
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    let running = false;
    const timer = setInterval(() => {
      if (running) return;
      running = true;
      void check(controller.signal).catch(() => { /* Keep uncertain outcome; never resubmit automatically. */ })
        .finally(() => { running = false; });
    }, 5000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [check, pending]);
  const confirm = async () => {
    if (action.current || !chosen || (!pending && !acknowledged)) return;
    action.current = true; setBusy(true); setError("");
    let sent = false;
    try {
      localStorage.setItem(storageKey, chosen);
      setPending(chosen);
      sent = true;
      accept(await recoveryRequest<Result>(endpoint, undefined, "POST", { recovery_id: chosen, confirm: true }));
    } catch (failure) {
      if (sent) {
        try { if ((await check()).status === "retired") return; } catch { /* Remain pending. */ }
      }
      if (live.current) setError(sent ? "收尾结果尚未确认。" + recoveryError(failure) + "可查询结果，或重试同一次收尾。"
        : "无法保存本次确认，尚未发送请求。请检查浏览器存储设置。");
    } finally { action.current = false; if (live.current) setBusy(false); }
  };
  if (!eligible.length && !pending && result?.status !== "retired") return null;
  return <section className="terminal-retirement" aria-label="结束旧会话记录">
    <h4>结束旧会话记录</h4>
    {result?.status === "retired" ? <div>
      <p role="status">旧会话记录已收尾，恢复快照已保留。项目文件不会因收尾而改变。</p>
      <p className="terminal-recovery-muted">收尾时间：{recoveryDate(result.observed_at!)}。原退出码：{result.original_exit_code ?? "未知"}。</p>
      {result.staging_retained && result.recovery_id && <TerminalStagingCleanup session={session} recovery={result.recovery_id} />}
      {result.recovery_id && <TerminalImageCleanup key={session + result.recovery_id} session={session} recovery={result.recovery_id} />}
    </div> : <>
      <p>选择已保存的快照，确认原进程退出后结束旧记录，让项目可以继续校验。快照和原沙箱副本会保留；需要的文件可另行写回。</p>
      <div className="terminal-recovery-selection">
        <label htmlFor={selectId}>收尾保留的快照</label>
        <select id={selectId} value={chosen} disabled={busy || !!pending || checking} onChange={event => {
          setSelected(event.target.value); setAcknowledged(false);
        }}>
          {pending && !eligible.some(row => row.id === pending) && <option value={pending}>上次确认的快照 {pending.slice(0, 12)}</option>}
          {eligible.map(row => <option key={row.id} value={row.id}>{recoveryDate(row.created_at)} · {row.id.slice(0, 12)}</option>)}
        </select>
      </div>
      {!pending && <label className="terminal-retirement-confirm"><input type="checkbox" checked={acknowledged}
        disabled={busy || checking} onChange={event => setAcknowledged(event.target.checked)} />
        我已核对保留的快照，确认结束旧会话记录。
      </label>}
      {checking && <p role="status">正在查询收尾记录…</p>}
      {pending && <p role="status">正在核对上次确认的结果。关闭后重新打开会继续查询。</p>}
      {error && <p className="terminal-recovery-error" role="alert">{error}</p>}
      <div className="terminal-recovery-actions">
        <button type="button" onClick={() => void confirm()} disabled={busy || checking || (!pending && !acknowledged)}>
          {busy ? "正在核验并收尾…" : pending ? "重试同一次收尾" : "确认结束旧会话"}
        </button>
        <button type="button" disabled={busy || checking} onClick={() => {
          setChecking(true); setError(""); void check().catch(failure => { if (live.current) setError(recoveryError(failure)); });
        }}>查询收尾结果</button>
      </div>
    </>}
  </section>;
}
