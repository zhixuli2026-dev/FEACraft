import { useCallback, useEffect, useRef, useState } from "react";
import { recoveryError, recoveryRequest } from "../utils/terminalRecovery";

type Result = { status: "not_started" | "pending" | "completed" | "not_retired" | "not_applicable" };

function pendingConfirmation(key: string, recovery: string) {
  try { return localStorage.getItem(key) === recovery; } catch { return false; }
}

export default function TerminalStagingCleanup({ session, recovery }: { session: string; recovery: string }) {
  const endpoint = "/terminal/sessions/" + encodeURIComponent(session) + "/staging-cleanup";
  const storageKey = "vf_terminal_staging_cleanup:" + session;
  const [pending, setPending] = useState(() => pendingConfirmation(storageKey, recovery));
  const [result, setResult] = useState<Result | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  const action = useRef(false);
  const accept = useCallback((value: Result) => {
    if (!live.current) return;
    setResult(value);
    if (value.status === "completed") {
      setPending(false); setError("");
      try { localStorage.removeItem(storageKey); } catch { /* Completion comes from the server. */ }
    }
    if (value.status === "pending") setPending(true);
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
    void check(controller.signal).catch(failure => {
      if (!controller.signal.aborted) setError(recoveryError(failure));
    });
    return () => { live.current = false; controller.abort(); };
  }, [check]);
  useEffect(() => {
    if (!pending) return;
    const controller = new AbortController();
    let running = false;
    const timer = setInterval(() => {
      if (running) return;
      running = true;
      void check(controller.signal).catch(() => { /* Query only; never automatically delete. */ })
        .finally(() => { running = false; });
    }, 5000);
    return () => { clearInterval(timer); controller.abort(); };
  }, [check, pending]);
  const confirm = async () => {
    if (action.current || (!pending && !acknowledged)) return;
    action.current = true; setBusy(true); setError("");
    let sent = false;
    try {
      localStorage.setItem(storageKey, recovery);
      setPending(true); sent = true;
      accept(await recoveryRequest<Result>(endpoint, undefined, "POST", { recovery_id: recovery, confirm: true }));
    } catch (failure) {
      if (sent) {
        try { if ((await check()).status === "completed") return; } catch { /* Outcome remains uncertain. */ }
      }
      if (live.current) setError(sent ? "暂存清理结果尚未确认。" + recoveryError(failure) + "请查询结果，或重试同一次清理。"
        : "无法保存本次确认，尚未发送请求。请检查浏览器存储设置。");
    } finally { action.current = false; if (live.current) setBusy(false); }
  };
  return <section className="terminal-retirement" aria-label="清理旧会话暂存">
    <h4>清理旧会话暂存</h4>
    {result?.status === "completed" ? <p role="status">旧会话的下载暂存已清理，恢复快照仍保留。</p> : <>
      <p>清除旧会话下载的本地副本，解除这份暂存对移动等管理操作的阻塞。需要的沙箱文件仍可从保留快照写回。</p>
      <p className="terminal-recovery-muted">清理前会核验快照和暂存内容。发现新增或变化的文件时会停止，请保留现场并处理这些文件后重试。</p>
      {!pending && <label className="terminal-retirement-confirm"><input type="checkbox" checked={acknowledged}
        disabled={busy || checking} onChange={event => setAcknowledged(event.target.checked)} />
        我确认清理旧会话的下载暂存，保留恢复快照。
      </label>}
      {checking && <p role="status">正在查询暂存清理记录…</p>}
      {pending && <p role="status">上次清理尚待确认，关闭后重新打开会继续查询。重试将核对并清理剩余内容。</p>}
      {error && <p role="alert" className="terminal-recovery-error">{error}</p>}
      <div className="terminal-recovery-actions">
        <button type="button" disabled={busy || checking || (!pending && !acknowledged)} onClick={() => void confirm()}>
          {busy ? "正在核验并清理…" : pending ? "重试同一次暂存清理" : "确认清理下载暂存"}
        </button>
        <button type="button" disabled={busy || checking} onClick={() => {
          setChecking(true); setError(""); void check().catch(failure => { if (live.current) setError(recoveryError(failure)); });
        }}>查询暂存清理结果</button>
      </div>
    </>}
  </section>;
}
