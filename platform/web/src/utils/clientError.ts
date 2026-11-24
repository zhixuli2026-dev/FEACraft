const recent = new Map<string, number>();

export function reportClientError(error: unknown) {
  const url = location.origin + location.pathname;
  const stripPageDetails = (text: string) => {
    const full = text.replaceAll(location.href, url);
    return location.search ? full.replaceAll(url + location.search, url) : full;
  };
  const message = stripPageDetails(error instanceof Error ? error.message : String(error));
  const stack = stripPageDetails(error instanceof Error ? error.stack ?? "" : "");
  const key = `${message}\n${stack.slice(0, 200)}`;
  const now = Date.now();
  if (now - (recent.get(key) ?? 0) < 5000) return;
  recent.set(key, now);
  const token = localStorage.getItem("vf_token");
  void fetch("/api/v1/runtime/client-error", {
    method: "POST",
    referrerPolicy: "no-referrer",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify({ message, stack, url, user_agent: navigator.userAgent }),
  }).catch(() => { /* 错误上报不得引发二次崩溃 */ });
}
