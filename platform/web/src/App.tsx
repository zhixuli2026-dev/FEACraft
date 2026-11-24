import { useEffect, useState } from "react";
import { api, ApiError, setToken } from "./api";
import Login from "./pages/Login";
import Shell from "./shell/Shell";
import { useStore } from "./store";
import { applyAppTheme } from "./utils/theme";
import { reportClientError } from "./utils/clientError";
import { chatgptOutcome, finishLegacyOrganizationLogin, oidcOutcome } from "./auth";

export default function App() {
  const { user, setUser, loadWorkspaces, theme } = useStore();
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState('');
  const [bootAttempt, setBootAttempt] = useState(0);

  useEffect(() => { applyAppTheme(theme); }, []);
  useEffect(() => {
    const onError = (event: ErrorEvent) => reportClientError(event.error ?? event.message);
    const onRejection = (event: PromiseRejectionEvent) => reportClientError(event.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);
  useEffect(() => {
    if (finishLegacyOrganizationLogin()) return;
    let active = true;
    setBooting(true); setBootError('');
    (async () => {
      let accountVerified = false;
      // A successful cookie login supersedes any legacy token from this browser.
      if (chatgptOutcome === "complete" || oidcOutcome === "complete") setToken(null);
      try {
        const me = await api.get("/auth/me");
        if (!active) return;
        setUser(me);
        accountVerified = true;
        await loadWorkspaces();
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          setToken(null);
          setUser(null);
        } else {
          setBootError(accountVerified
            ? '账户已验证，工作区暂时无法读取。请重试加载。'
            : '暂时无法验证账户。请检查网络后重试。');
        }
      } finally {
        if (active) setBooting(false);
      }
    })();
    return () => { active = false; };
  }, [bootAttempt, loadWorkspaces, setUser]);

  if (booting) return <div style={s.boot}>加载中…</div>;
  if (bootError) return <div style={s.boot}>
    <div className="auth-surface auth-notice" role="alert" data-testid="bootstrap-error">
      <span>{bootError}</span><button type="button" className="auth-link" data-testid="bootstrap-retry"
        style={{ minHeight: 44, fontSize: 16 }} onClick={() => setBootAttempt(value => value + 1)}>重试加载</button>
    </div>
  </div>;
  return user ? <Shell /> : <Login />;
}

const s: Record<string, React.CSSProperties> = {
  boot: { minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, background: "var(--vf-bg)", color: "var(--vf-text)", fontSize: 16, lineHeight: 1.6 },
};
