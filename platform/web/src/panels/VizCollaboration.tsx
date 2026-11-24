import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { api, ApiError } from "../api";
import "./VizCollaboration.css";

type Role = { id: number; name: string };
type Presentation = { active: true; presenter: Role; session_id: number; version: string; sequence: number; viewers: Role[] };
type Status = { active: false } | Presentation;
type Publisher = { version: string; sequence: number; sessionId: number };

async function request(path: string, options: RequestInit) {
  const token = localStorage.getItem("vf_token");
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(response.status, error.detail || "可视化协作请求未完成");
  }
  return response;
}

async function capture(frame: HTMLIFrameElement | null) {
  const source = frame?.contentDocument?.querySelector("canvas");
  if (!source?.width || !source.height) return null;
  const scale = Math.min(1, 960 / source.width, 720 / source.height);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(source.width * scale));
  canvas.height = Math.max(1, Math.round(source.height * scale));
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", .76));
}

export default function VizCollaboration({ caseId, userId, canPresent, sessionId, iframe }: {
  caseId: number; userId: number; canPresent: boolean; sessionId: number | null;
  iframe: RefObject<HTMLIFrameElement | null>;
}) {
  const endpoint = `/cases/${caseId}/viz:collaboration`;
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [publisher, setPublisher] = useState<Publisher | null>(null);
  const [following, setFollowing] = useState(false);
  const [picture, setPicture] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pictureRef = useRef("");
  const sequenceRef = useRef({ version: "", sequence: 0 });

  useEffect(() => {
    if (!open && !publisher) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const snapshot = await api.get<Status>(endpoint, undefined, controller.signal);
        if (!controller.signal.aborted) setStatus(snapshot);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setStatus(null);
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 900);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [endpoint, open, publisher]);

  useEffect(() => {
    if (!publisher) return;
    const controller = new AbortController();
    let sequence = publisher.sequence;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const publish = async () => {
      try {
        if (publisher.sessionId !== sessionId || !canPresent) {
          await request(`${endpoint}?version=${publisher.version}`, { method: "DELETE", signal: controller.signal });
          setPublisher(null);
          return;
        }
        const jpeg = await capture(iframe.current);
        if (jpeg && !controller.signal.aborted) {
          await request(`${endpoint}/frame?version=${publisher.version}&sequence=${sequence + 1}`, {
            method: "PUT", headers: { "Content-Type": "image/jpeg" }, body: jpeg, signal: controller.signal,
          });
          sequence += 1;
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          setPublisher(null);
          setError(cause instanceof Error ? cause.message : String(cause));
        }
        return;
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void publish(), 750);
    };
    void publish();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [publisher, endpoint, sessionId, iframe, canPresent]);

  const version = status?.active ? status.version : "";
  const currentSequence = status?.active ? status.sequence : 0;
  const canResume = status?.active && status.presenter.id === userId && status.session_id === sessionId;
  useEffect(() => {
    if (pictureRef.current) URL.revokeObjectURL(pictureRef.current);
    pictureRef.current = "";
    sequenceRef.current = { version, sequence: 0 };
    setPicture("");
    if (!open || !following || !version) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const join = async () => {
      try {
        await request(`${endpoint}/presence?version=${version}`, { method: "POST", signal: controller.signal });
      } catch (cause) {
        if (!controller.signal.aborted && !(cause instanceof ApiError && cause.status === 409))
          setError(cause instanceof Error ? cause.message : String(cause));
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void join(), 4000);
    };
    void join();
    return () => {
      controller.abort(); clearTimeout(timer);
      if (pictureRef.current) URL.revokeObjectURL(pictureRef.current);
      pictureRef.current = "";
    };
  }, [endpoint, following, open, version]);

  useEffect(() => {
    if (!open || !following || !version || currentSequence <= sequenceRef.current.sequence) return;
    const controller = new AbortController();
    void request(`${endpoint}/frame?version=${version}&after=${sequenceRef.current.sequence}`, { signal: controller.signal })
      .then(async response => {
        if (response.status === 204) return;
        const blob = await response.blob();
        if (controller.signal.aborted || sequenceRef.current.version !== version) return;
        const old = pictureRef.current;
        sequenceRef.current.sequence = Number(response.headers.get("X-Frame-Sequence"));
        pictureRef.current = URL.createObjectURL(blob);
        setPicture(pictureRef.current);
        if (old) URL.revokeObjectURL(old);
      }).catch(cause => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, [endpoint, following, open, version, currentSequence]);

  async function begin() {
    if (!sessionId || busy) return;
    setBusy(true); setError("");
    try {
      if (canResume && status.active) {
        setPublisher({ version: status.version, sequence: status.sequence, sessionId });
        return;
      }
      const state = await api.post<{ version: string; sequence: number }>(`${endpoint}:start`, { session_id: sessionId });
      setPublisher({ ...state, sessionId });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  async function stop() {
    if (!publisher || busy) return;
    setBusy(true); setError("");
    const version = publisher.version;
    setPublisher(null);
    try {
      await request(`${endpoint}?version=${version}`, { method: "DELETE" });
      setStatus({ active: false });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  return <details className="vf-viz-sharing" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>协作查看</summary>
    {open && <div className="vf-viz-sharing-body">
      {status?.active ? <p role="status">{status.presenter.name} 正在共享 · {status.viewers.length} 人观看</p>
        : <p role="status">{status ? "当前没有共享画面" : "正在读取共享状态…"}</p>}
      <div className="vf-viz-sharing-actions">
        {publisher ? <button type="button" disabled={busy} onClick={() => void stop()}>停止共享</button>
          : canPresent && <button type="button" disabled={busy || !sessionId || (Boolean(status?.active) && !canResume)}
            onClick={() => void begin()}>{canResume ? "继续共享" : "共享当前画面"}</button>}
        {status?.active && status.presenter.id !== userId && <button type="button" aria-pressed={following}
          onClick={() => { setError(""); setFollowing(value => !value); }}>{following ? "停止跟随" : "跟随画面"}</button>}
      </div>
      {status?.active && status.viewers.length > 0 && <p>观看者 {status.viewers.map(viewer => viewer.name).join("、")}</p>}
      {following && status?.active && (picture ? <img alt="共享的可视化画面" src={picture} /> : <p>等待共享画面…</p>)}
      {error && <p role="alert">{error}</p>}
    </div>}
  </details>;
}
