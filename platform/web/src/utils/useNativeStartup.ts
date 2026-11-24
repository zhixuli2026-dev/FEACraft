import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';

export type NativeRuntime = 'windows' | 'wsl' | 'docker';
export interface NativeSessionState { id: string; phase: string; cleanup_verified: boolean | null }
export interface NativeStartup {
  id: string; client_request_id: string; phase: string; runtime: string; purpose: 'chat' | 'inspect';
  session: NativeSessionState | null; cleanup_verified: boolean | null; error?: string;
}
interface StartupRequest {
  client_request_id: string; provider_profile_id: number; provider_wire_api: 'responses';
  runtime: NativeRuntime; purpose: 'chat' | 'inspect';
}
export const startupReady = (value: NativeStartup | null) => value?.phase === 'completed' && value.session?.phase === 'active';
export const startupSettled = (value: NativeStartup | null) => !!value && value.cleanup_verified === true &&
  (['failed', 'cancelled'].includes(value.phase) || value.phase === 'completed' && !!value.session && ['closed', 'failed'].includes(value.session.phase));
export const startupResponseUnknown = '启动响应尚未确认，正在查询原请求；消息尚未发送';

/** Persist identifiers and selection only. Refresh never starts a CLI or sends a message. */
export default function useNativeStartup(userId: number, cid: number) {
  const key = `vf-native-startup:${userId}:${cid}`;
  const prefix = `/codex/conversations/${cid}/session-requests`;
  const [pending, setPending] = useState<StartupRequest | null>(() => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
      return value && typeof value.client_request_id === 'string' && ['chat', 'inspect'].includes(value.purpose) &&
        ['windows', 'wsl', 'docker'].includes(value.runtime) && Number.isInteger(value.provider_profile_id) ? value : null;
    } catch { return null; }
  });
  const [state, setState] = useState<NativeStartup | null>(null);
  const requestRef = useRef(pending);
  const stopped = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const remember = (value: StartupRequest | null) => {
    if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key);
    requestRef.current = value;
    if (mounted.current) { setPending(value); if (!value) setState(null); }
  };
  const observe = (value: NativeStartup) => {
    if (mounted.current && requestRef.current?.client_request_id === value.client_request_id) setState(value);
    return value;
  };
  const refresh = async () => {
    const request = requestRef.current;
    if (!request) return null;
    const found = await api.get<NativeStartup[]>(prefix + `?client_request_id=${encodeURIComponent(request.client_request_id)}`);
    return found[0] ? observe(found[0]) : null;
  };
  const post = async (request: StartupRequest) => {
    try { return observe(await api.post<NativeStartup>(prefix, request)); }
    catch (e) {
      if (e instanceof ApiError && [400, 401, 403, 404, 422, 429].includes(e.status)) remember(null);
      throw e instanceof ApiError ? e : new Error(startupResponseUnknown);
    }
  };
  const create = async (purpose: 'chat' | 'inspect', profileId: number, runtime: NativeRuntime) => {
    stopped.current = null;
    let request = requestRef.current;
    let value: NativeStartup | null;
    if (request) {
      if (request.purpose !== purpose || request.runtime !== runtime || request.provider_profile_id !== profileId)
        throw new Error('请先关闭上次启动的会话，再更换用途或运行配置');
      value = await refresh();
    } else {
      request = { client_request_id: crypto.randomUUID(), provider_profile_id: profileId, provider_wire_api: 'responses', runtime, purpose };
      stopped.current = null; remember(request);
      value = await post(request);
    }
    const deadline = Date.now() + 55000;
    while (value && ['admitted', 'starting'].includes(value.phase) && mounted.current && stopped.current !== request.client_request_id && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      value = observe(await api.get<NativeStartup>(`/codex/session-requests/${value.id}`));
    }
    if (!mounted.current || stopped.current === request.client_request_id) throw new Error('已停止等待启动；消息尚未发送');
    if (!value || !startupReady(value)) throw new Error('CLI 尚未准备完成，请查看启动状态；消息尚未发送');
    return value.session!;
  };
  return { pending, state, refresh, create, consume: () => remember(null),
    retry: async () => { if (requestRef.current) await post(requestRef.current); },
    cancel: async (value: NativeStartup) => {
      stopped.current = value.client_request_id;
      observe(await api.post<NativeStartup>(`/codex/session-requests/${value.id}:cancel`));
    },
    close: async () => {
      const current = await refresh();
      if (current && startupReady(current)) {
        await api.del(`/codex/sessions/${current.session!.id}`);
        const closed = await refresh();
        if (!startupSettled(closed)) throw new Error('CLI 的关闭结果尚未核验，请继续读取状态');
      } else if (!startupSettled(current)) throw new Error('启动结果尚未核验，请继续读取状态');
      remember(null);
    },
  };
}
