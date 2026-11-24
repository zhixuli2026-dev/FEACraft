import { useEffect, useState } from "react";
import { api, ApiError, setToken } from "../api";
import { useStore } from "../store";
import { oidcOutcome, organizationLoginMessage, startOrganizationLogin } from "../auth";
import type { OrganizationProvider } from "../auth";
import WebsiteSignIn from "./WebsiteSignIn";
import "../auth.css";

interface Connection { id: string; email: string | null; email_verified: boolean; name?: string }
type AccountConnection = Connection & { source: "chatgpt" | "oidc" };

export default function AccountSettings() {
  const user = useStore((s) => s.user);
  const [connections, setConnections] = useState<AccountConnection[] | null>(null);
  const [providers, setProviders] = useState<OrganizationProvider[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(oidcOutcome && oidcOutcome!=="complete" ? organizationLoginMessage(oidcOutcome) : "");
  const [removing, setRemoving] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const load = async () => {
    try {
      const [chatgpt, oidc, available] = await Promise.all([
        api.get<Connection[]>("/auth/chatgpt/connections"),
        api.get<Connection[]>("/auth/oidc/connections"),
        api.get<OrganizationProvider[]>("/auth/oidc/providers"),
      ]);
      setConnections([...chatgpt.map(item => ({...item,source:"chatgpt" as const})),
        ...oidc.map(item => ({...item,source:"oidc" as const}))]);
      setProviders(available);
    }
    catch { setError("无法读取账户关联，请重新加载。"); }
  };
  useEffect(() => { void load(); }, []);
  const logout = async () => {
    setBusy(true); setError("");
    try {
      const result = await api.post<{logout_url?:string}>("/auth/logout", {});
      setToken(null); window.location.assign(result.logout_url ?? "/");
    } catch { setError("退出未完成，请重试。"); setBusy(false); }
  };
  const unlink = async (connection: AccountConnection) => {
    setBusy(true); setError(""); setNotice("");
    try {
      await api.del(`/auth/${connection.source}/connections/${connection.id}`);
      setRemoving(null);
      try { await api.get("/auth/me"); }
      catch (e) {
        if (e instanceof ApiError && e.status === 401) { setToken(null); window.location.assign("/"); return; }
        throw e;
      }
      await load(); setNotice("已解除关联，该身份的登录会话已退出。");
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? "这是唯一的登录方式，暂不能解除关联。" : "未能解除关联，请重新加载账户状态后重试。");
    } finally { setBusy(false); }
  };
  const link = async (id: string) => {
    setBusy(true); setError("");
    try { await startOrganizationLogin(id,"link"); }
    catch { setBusy(false); setError("无法开始组织账户关联，请重试。"); }
  };
  return <section className="account-settings auth-surface" aria-labelledby="account-title">
    <h2 id="account-title">账户与登录</h2>
    <p className="account-email">{user?.email}</p>
    <p className="auth-muted">账户关联为当前平台账户添加登录方式。</p>
    {connections === null ? <button type="button" className="auth-link" onClick={load}>重新加载账户关联</button> : connections.length === 0 ?
      <p className="auth-muted">尚未关联其他登录方式。</p> : connections.map((c) => <div className="account-connection" key={c.id}>
        <p>{c.name ?? "ChatGPT"} · {c.email || "已关联账户"}</p>
        {removing === c.id ? <>
          <p className="auth-muted">解除关联后，通过此身份登录的会话将退出。</p>
          <div className="account-actions">
            <button type="button" className="auth-button auth-secondary" disabled={busy} onClick={() => unlink(c)}>确认解除关联</button>
            <button type="button" className="auth-link" disabled={busy} onClick={() => setRemoving(null)}>保留关联</button>
          </div>
        </> : <button type="button" className="auth-link" disabled={busy} onClick={() => setRemoving(c.id)}>解除关联</button>}
      </div>)}
    <WebsiteSignIn intent="link" disabled={busy} />
    {providers.map(item => <button type="button" className="auth-button auth-secondary" key={item.id}
      disabled={busy} onClick={() => link(item.id)}>关联{item.name}</button>)}
    <button type="button" className="auth-button auth-secondary" disabled={busy} onClick={logout}>{busy ? "正在处理…" : "退出登录"}</button>
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert" className="auth-error">{error}</p>}
  </section>;
}
