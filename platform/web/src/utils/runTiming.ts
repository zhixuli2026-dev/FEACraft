export interface RunTiming {
  runId: string;
  status: string;
  startedAt?: number;
  endedAt?: number;
  durationMs?: number;
}

export function projectRunTimings(events: any[]): Record<string, RunTiming> {
  const result: Record<string, RunTiming> = {};
  for (const event of events) {
    if (event.type !== "run_state" || !event.payload?.run_id) continue;
    const payload = event.payload;
    const runId = String(payload.run_id);
    const previous = result[runId];
    const startedAt = Number.isFinite(payload.started_at)
      ? payload.started_at * 1000 : previous?.startedAt;
    const endedAt = Number.isFinite(payload.ended_at)
      ? payload.ended_at * 1000 : previous?.endedAt;
    const durationMs = Number.isFinite(payload.duration_ms)
      ? Math.max(0, payload.duration_ms)
      : startedAt !== undefined && endedAt !== undefined
        ? Math.max(0, endedAt - startedAt) : previous?.durationMs;
    result[runId] = {runId, status: payload.status ?? previous?.status ?? "unknown",
      startedAt, endedAt, durationMs};
  }
  return result;
}

export function runDurationMs(timing: RunTiming, now = Date.now()): number {
  if (timing.durationMs !== undefined) return timing.durationMs;
  if (timing.startedAt === undefined) return 0;
  return Math.max(0, (timing.endedAt ?? now) - timing.startedAt);
}

export function formatRunDuration(milliseconds: number): string {
  const ms = Math.max(0, milliseconds);
  if (ms < 1000) return "<1 秒";
  if (ms < 10000) return `${(ms / 1000).toFixed(1)} 秒`;
  if (ms < 60000) return `${Math.round(ms / 1000)} 秒`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  if (minutes < 60) return `${minutes} 分 ${seconds} 秒`;
  return `${Math.floor(minutes / 60)} 时 ${minutes % 60} 分`;
}
