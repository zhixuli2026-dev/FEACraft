import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { startWebsiteLogin } from "../auth";
import type { WebsiteLoginStatus } from "../auth";
import "../auth.css";

export default function WebsiteSignIn({ intent, disabled = false }: { intent: "login" | "link"; disabled?: boolean }) {
  const [status, setStatus] = useState<WebsiteLoginStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(() => {
    api.get<WebsiteLoginStatus>("/auth/chatgpt/status").then(setStatus)
      .catch(() => setError("暂时无法查询 ChatGPT 登录状态，请重试。"));
  }, []);
  useEffect(() => {
    load();
    const onShow = () => { setBusy(false); load(); };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, [load]);
  const pending = status?.pending && ["pending", "processing"].includes(status.pending.phase);
  const run = async (cancel = false) => {
    setBusy(true); setError(""); setNotice("");
    try {
      if (cancel) {
        await api.post("/auth/chatgpt/cancel", {});
        setNotice("已取消此次 ChatGPT 登录。"); load(); setBusy(false);
      } else await startWebsiteLogin(intent);
    } catch {
      setError("暂时无法连接 ChatGPT 登录服务，请稍后重试。"); setBusy(false);
    }
  };
  return <div className="website-signin auth-surface">
    {status?.enabled && <>
      <button className="auth-button auth-secondary" type="button" disabled={busy || disabled} onClick={() => run()}>
        {busy ? "正在处理…" : intent === "link" ? "关联 ChatGPT 账户" : "使用 ChatGPT 登录"}
      </button>
      {pending && <div className="auth-pending">
        <p>有一次尚未完成的登录。重新开始会替换它，也可以取消。</p>
        <button className="auth-link" type="button" disabled={busy || disabled} onClick={() => run(true)}>取消待完成的登录</button>
      </div>}
    </>}
    {status && !status.enabled && intent === "link" && <p className="auth-muted">管理员尚未启用 ChatGPT 登录。</p>}
    {notice && <p role="status" className="auth-muted">{notice}</p>}
    {error && <div role="alert" className="auth-error">{error} {!status && <button type="button" className="auth-link" onClick={() => { setError(""); load(); }}>重新查询</button>}</div>}
  </div>;
}
