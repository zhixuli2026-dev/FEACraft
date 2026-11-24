import { api } from "./api";

export interface WebsiteLoginStatus {
  enabled: boolean;
  pending?: { phase: string; intent: string; expires_at: string } | null;
}

// The callback contains only an outcome code; credentials stay in HttpOnly cookies.
const callbackUrl = new URL(window.location.href);
export const chatgptOutcome = callbackUrl.searchParams.get("chatgpt");
export const oidcOutcome = callbackUrl.searchParams.get("oidc");
const legacyOidc = callbackUrl.pathname === "/oidc/callback"
  ? {code: callbackUrl.searchParams.get("code"), state: callbackUrl.searchParams.get("state")} : null;
if (legacyOidc) window.history.replaceState({}, "", "/");
if (chatgptOutcome || oidcOutcome) {
  callbackUrl.searchParams.delete("chatgpt");
  callbackUrl.searchParams.delete("oidc");
  window.history.replaceState({}, "", callbackUrl.pathname + callbackUrl.search + callbackUrl.hash);
}

export function loginMessage(code: string): string {
  const messages: Record<string, string> = {
    complete: "ChatGPT 账户验证已完成。",
    access_denied: "你取消了 ChatGPT 授权，可以重新登录或使用邮箱密码。",
    account_link_required: "此邮箱已有账户。请先用原有方式登录，再到设置中关联 ChatGPT。",
    account_setup_required: "请先用邮箱注册账户，再到设置中关联 ChatGPT。",
    identity_already_linked: "这个 ChatGPT 账户已关联其他平台账户，请换一个账户重试。",
    account_unavailable: "平台账户暂不可用，请联系管理员。",
    configuration_changed: "登录配置已更新，请重新发起登录。",
    state_invalid: "此次登录已失效或已处理，请重新发起登录。",
    transaction_unavailable: "此次登录已取消或过期，请重新发起登录。",
    identity_invalid: "未能验证 ChatGPT 身份，请重新登录。若仍失败，请联系管理员。",
  };
  return messages[code] ?? "ChatGPT 登录未完成，请重试或使用邮箱密码登录。";
}

export async function startWebsiteLogin(intent: "login" | "link") {
  const result = await api.post<{ authorization_url: string }>("/auth/chatgpt/start", { intent });
  window.location.assign(result.authorization_url);
}

export interface OrganizationProvider {
  id: string;
  name: string;
}

export function organizationLoginMessage(code: string) {
  const messages: Record<string, string> = {
    complete: "组织账户验证已完成。",
    account_link_required: "此邮箱已有账户。请用原有方式登录，再到设置中关联组织账户。",
    identity_already_linked: "此组织账户已关联其他平台账户。",
    identity_email_unverified: "请先在组织身份平台验证邮箱，再登录。",
    identity_invalid: "组织身份验证失败，请重新发起登录。",
    state_invalid: "此次登录已失效或已处理，请重新发起登录。",
    configuration_changed: "登录配置已更新，请重新发起登录。",
    access_denied: "你取消了组织授权。"
  };
  return messages[code] ?? "组织登录未完成，请重试或使用原有登录方式。";
}

export async function startOrganizationLogin(id: string, intent: "login" | "link") {
  const result = await api.post<{ authorization_url: string }>(`/auth/oidc/${encodeURIComponent(id)}/start`, { intent });
  window.location.assign(result.authorization_url);
}

let legacyCompletionStarted = false;
export function finishLegacyOrganizationLogin() {
  if (!legacyOidc) return false;
  if (!legacyCompletionStarted) {
    legacyCompletionStarted = true;
    const finish = legacyOidc.code && legacyOidc.state
      ? api.post("/auth/oidc/default/callback", legacyOidc).then(() => "complete", () => "authorization_failed")
      : Promise.resolve("authorization_failed");
    void finish.then(outcome => window.location.replace(`/?oidc=${outcome}`));
  }
  return true;
}
