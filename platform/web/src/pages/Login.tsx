import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, setToken } from "../api";
import { useStore } from "../store";
import WebsiteSignIn from "../components/WebsiteSignIn";
import { chatgptOutcome, loginMessage, oidcOutcome, organizationLoginMessage, startOrganizationLogin } from "../auth";
import type { OrganizationProvider } from "../auth";

export default function Login() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState(oidcOutcome && oidcOutcome !== "complete" ? organizationLoginMessage(oidcOutcome) :
    chatgptOutcome && chatgptOutcome !== "complete" ? loginMessage(chatgptOutcome) : "");
  const [busy, setBusy] = useState(false);
  const [oidcProviders, setOidcProviders] = useState<OrganizationProvider[]>([]);
  const setUser = useStore((s) => s.setUser);
  const loadWorkspaces = useStore((s) => s.loadWorkspaces);

  useEffect(() => {
    api.get<OrganizationProvider[]>("/auth/oidc/providers").then(setOidcProviders).catch(() => {});
  }, []);

  const startOidc = async (id: string) => {
    setErr(""); setBusy(true);
    try {
      await startOrganizationLogin(id, "login");
    } catch (error: any) {
      setErr(String(error?.message ?? error)); setBusy(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      const r = await api.post(
        `/auth/${mode}`,
        mode === "register" ? { email, name, password } : { email, password }
      );
      setToken(r.token);
      setUser(r.user);
      await loadWorkspaces();
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-wrap auth-surface">
      <form onSubmit={submit} className="login-card" aria-label={mode === "login" ? "登录账户" : "注册账户"} aria-busy={busy}>
        <h1>VibeFlow Platform</h1>
        <p className="auth-muted login-subtitle">数值仿真工作台 · Web 版</p>
        {mode === "register" && (
          <label className="login-field">昵称<input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} /></label>
        )}
        <label className="login-field">邮箱<input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <label className="login-field">密码<input type="password" required minLength={mode === "register" ? 8 : undefined}
          placeholder={mode === "register" ? "至少 8 位" : undefined} autoComplete={mode === "register" ? "new-password" : "current-password"}
          value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {err && <div role="alert" className="auth-error">{err}</div>}
        <button className="auth-button" disabled={busy}>
          {busy ? "正在处理…" : mode === "login" ? "登录" : "注册"}
        </button>
        <WebsiteSignIn intent="login" disabled={busy} />
        {oidcProviders.map((provider) => (
          <button data-testid="oidc-login" type="button" key={provider.id} className="auth-button auth-secondary"
                  disabled={busy} onClick={() => startOidc(provider.id)}>使用{provider.name}登录</button>
        ))}
        <button className="auth-link" type="button" disabled={busy} onClick={() => { setMode(mode === "login" ? "register" : "login"); setErr(""); }}>
          {mode === "login" ? "没有账号？注册" : "已有账号？登录"}
        </button>
      </form>
    </main>
  );
}

