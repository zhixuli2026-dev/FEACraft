import { useStore } from '../store';
import { readNativeEvents, type NativeTurn } from './nativeChat';

let audioContext: AudioContext | null = null;

const preferenceKey = (userId: number) => `vf-agent-reply-sound:${userId}`;

export function replySoundEnabled(userId?: number) {
  return userId != null && localStorage.getItem(preferenceKey(userId)) === 'on';
}

export function setReplySoundEnabled(userId: number, enabled: boolean) {
  localStorage.setItem(preferenceKey(userId), enabled ? 'on' : 'off');
  if (enabled) prepareReplyAudio(userId);
}

export function prepareReplyAudio(userId?: number) {
  if (!replySoundEnabled(userId)) return;
  try {
    if (!audioContext || audioContext.state === 'closed') audioContext = new AudioContext();
    if (audioContext.state === 'suspended') void audioContext.resume().catch(() => {});
  } catch { /* Device or browser audio can be unavailable. */ }
}

export function playReplySound(userId?: number) {
  if (useStore.getState().user?.id !== userId || !replySoundEnabled(userId)) return;
  try {
    if (audioContext?.state !== 'running') return;
    const t0 = audioContext.currentTime;
    for (const [frequency, delay] of [[880, 0], [1318, 0.09]] as const) {
      const osc = audioContext.createOscillator();
      const gain = audioContext.createGain();
      osc.frequency.value = frequency;
      gain.gain.setValueAtTime(0.06, t0 + delay);
      gain.gain.exponentialRampToValueAtTime(0.001, t0 + delay + 0.12);
      osc.connect(gain).connect(audioContext.destination);
      osc.onended = () => { osc.disconnect(); gain.disconnect(); };
      osc.start(t0 + delay);
      osc.stop(t0 + delay + 0.13);
    }
  } catch { /* Device or browser audio can be unavailable. */ }
}

/** Follow a turn submitted by this document, even while its panel is detached. */
export async function observeNativeReplySound(userId: number, turn: NativeTurn) {
  const token = localStorage.getItem('vf_token');
  if (!token || useStore.getState().user?.id !== userId) return;
  const controller = new AbortController();
  const unsubscribe = useStore.subscribe(state => {
    if (state.user?.id !== userId) controller.abort();
  });
  let after = Math.max(0, turn.last_seq - 1);
  let delay = 500;
  let ended = false;
  try {
    while (!ended && !controller.signal.aborted) {
      if (localStorage.getItem('vf_token') !== token) return;
      try {
        const response = await fetch(`/api/v1/codex/turns/${encodeURIComponent(turn.id)}/events?after=${after}`, {
          signal: controller.signal, headers: { Authorization: `Bearer ${token}` },
        });
        if ([401, 403, 404].includes(response.status)) return;
        if (!response.ok) throw new Error('Native turn events unavailable');
        await readNativeEvents(response, event => {
          if (ended) return;
          if (event.type === 'execution.unavailable') { ended = true; return; }
          if (event.turn_id !== turn.id || event.seq !== after + 1) throw new Error('Native turn event gap');
          after = event.seq;
          delay = 500;
          if (event.type !== 'stream.done') return;
          ended = true;
          const { status, native_status, cleanup_verified } = event.payload;
          if ((status === 'completed' || status === 'waiting_continue') && native_status === 'completed' &&
              cleanup_verified === true && localStorage.getItem('vf_token') === token) playReplySound(userId);
        });
      } catch { /* Reconnect from the last accepted durable event. */ }
      if (!ended && !controller.signal.aborted) {
        await new Promise(resolve => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 10000);
      }
    }
  } finally { unsubscribe(); }
}
