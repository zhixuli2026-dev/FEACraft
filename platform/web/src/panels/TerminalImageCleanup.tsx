import { useCallback, useEffect, useRef, useState } from "react";
import { recoveryError, recoveryRequest } from "../utils/terminalRecovery";

type Result = { status: "not_started" | "pending" | "completed" };

function pendingConfirmation(key: string, recovery: string) {
  try { return localStorage.getItem(key) === recovery; } catch { return false; }
}

export default function TerminalImageCleanup({ session, recovery }: { session: string; recovery: string }) {
  const endpoint = "/terminal/sessions/" + encodeURIComponent(session) + "/image-cleanup";
  const storageKey = "vf_terminal_image_cleanup:" + session;
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
      if (live.current) setError(sent ? "镜像清理结果尚未确认。" + recoveryError(failure) + "请查询结果，或重试同一次清理。"
        : "无法保存本次确认，尚未发送请求。请检查浏览器存储设置。");
    } finally { action.current = false; if (live.current) setBusy(false); }
  };
  return <section className="terminal-retirement" aria-label="清理原沙箱镜像">
    <h4>清理原沙箱镜像</h4>
    {result?.status === "completed" ? <p role="status">原沙箱镜像已清理，恢复快照仍可下载和写回。</p> : <>
      <p>删除原沙箱的磁盘镜像以释放空间。删除后无法再从原镜像创建快照；已保留的恢复快照仍可下载和写回。</p>
      <p className="terminal-recovery-muted">清理前会核验保留快照、原镜像及相关进程是否退出。若镜像已变化或仍被占用，会停止清理。</p>
      {!pending && <label className="terminal-retirement-confirm"><input type="checkbox" checked={acknowledged}
        disabled={busy || checking} onChange={event => setAcknowledged(event.target.checked)} />
        我已核对保留快照，确认删除原沙箱镜像。
      </label>}
      {checking && <p role="status">正在查询镜像清理记录…</p>}
      {pending && <p role="status">上次清理尚待确认，关闭后重新打开会继续查询。重试将先核验上次执行是否已退出，再继续同一次清理。</p>}
      {error && <p role="alert" className="terminal-recovery-error">{error}</p>}
      <div className="terminal-recovery-actions">
        <button type="button" disabled={busy || checking || (!pending && !acknowledged)} onClick={() => void confirm()}>
          {busy ? "正在核验并清理…" : pending ? "重试同一次镜像清理" : "确认删除原沙箱镜像"}
        </button>
        <button type="button" disabled={busy || checking} onClick={() => {
          setChecking(true); setError(""); void check().catch(failure => { if (live.current) setError(recoveryError(failure)); });
        }}>查询镜像清理结果</button>
      </div>
    </>}
  </section>;
}
