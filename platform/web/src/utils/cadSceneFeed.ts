/** Versioned CAD notices reconnect independently of ordinary API and job streams. */
import { api, ApiError } from "../api.ts";

export type CadSceneNotice = {
  type: "scene.snapshot" | "scene.replace" | "scene.clear";
  case_id: number;
  version?: number;
  source?: "case" | "live";
};

export function acceptCadSceneNotice(value: unknown, caseId: number, version: number | null,
                                     firstOnConnection: boolean): value is CadSceneNotice {
  if (!value || typeof value !== "object") return false;
  const notice = value as CadSceneNotice;
  if (notice.case_id !== caseId || !["scene.snapshot", "scene.replace", "scene.clear"].includes(notice.type)) return false;
  if (notice.version === undefined) return version === null && notice.type !== "scene.snapshot";
  if (!Number.isSafeInteger(notice.version) || notice.version < 0) return false;
  if (notice.type === "scene.snapshot" && notice.source !== "case" && notice.source !== "live") return false;
  return version === null || notice.version > version
    || (notice.version === version && firstOnConnection && notice.type === "scene.snapshot");
}

export function followCadScenes(caseId: number, onNotice: (notice: CadSceneNotice) => void,
                                onError: (detail: string) => void) {
  let current: WebSocket | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let delay = 250;
  let stopped = false;
  let version: number | null = null;
  const reconnect = () => {
    if (stopped) return;
    timer = setTimeout(() => void connect(), delay);
    delay = Math.min(delay * 2, 4000);
  };

  const connect = async () => {
    let ticket: string;
    try {
      ({ ticket } = await api.post<{ ticket: string }>("/connection-tickets", { scope: "cadscene", case_id: caseId }));
    } catch (error) {
      if (stopped) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) onError(error.message);
      else reconnect();
      return;
    }
    if (stopped) return;
    const url = new URL("/ws/cadscene", window.location.href);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("case_id", String(caseId));
    url.searchParams.set("token", ticket);
    url.searchParams.set("snapshot", "true");
    const socket = new WebSocket(url);
    current = socket;
    let firstOnConnection = true;
    socket.onmessage = event => {
      if (stopped || current !== socket || typeof event.data !== "string") return;
      let message;
      try { message = JSON.parse(event.data); }
      catch { return; }
      if (message?.type === "ready" && message.case_id === caseId) delay = 250;
      else if (message?.type === "error") {
        const detail = typeof message.detail === "string" ? message.detail : message.detail?.message;
        if (typeof detail === "string") onError(detail);
      } else if (acceptCadSceneNotice(message, caseId, version, firstOnConnection)) {
        firstOnConnection = false;
        if (message.version !== undefined) version = message.version;
        onNotice(message);
      }
    };
    socket.onclose = event => {
      if (stopped || current !== socket) return;
      current = undefined;
      if ([4400, 4401, 4403].includes(event.code)) return;
      reconnect();
    };
  };
  void connect();
  return () => {
    stopped = true;
    if (timer !== undefined) clearTimeout(timer);
    current?.close();
  };
}
