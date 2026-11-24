import type { ParsedLog } from '../panels/ResidualChart';

type TimeSource = 'time' | 'time_step' | null;

export interface ResidualCursor {
  offset: number;
  fingerprint: string;
  unchanged: boolean;
  time_source: TimeSource;
}

export type ResidualDelta = ParsedLog & { cursor: ResidualCursor };

export interface ResidualHistory {
  data: ParsedLog;
  timeSource: TimeSource;
}

function appendFields<T>(previous: Record<string, T[]>, delta: Record<string, T[]>) {
  return { ...previous, ...Object.fromEntries(Object.entries(delta).map(([field, rows]) => [field,
    Object.hasOwn(previous, field) ? previous[field].concat(rows) : rows])) };
}

export function appendResiduals(previous: ResidualHistory | null, delta: ResidualDelta): ResidualHistory {
  const { cursor, ...next } = delta;
  if (!previous) return { data: next, timeSource: cursor.time_source };
  if (cursor.unchanged) return previous;

  const { data, timeSource } = previous;
  let times = data.times;
  if (cursor.time_source === 'time' && timeSource !== 'time') times = next.times;
  else if (cursor.time_source && (timeSource !== 'time' || cursor.time_source === 'time')) {
    times = times.concat(next.times);
  }
  const residuals = appendFields(data.residuals, next.residuals);
  const a = data.summary, b = next.summary;
  return {
    timeSource: timeSource === 'time' ? 'time' : cursor.time_source ?? timeSource,
    data: {
      times, residuals,
      courant: data.courant.concat(next.courant),
      residuals_detail: appendFields(data.residuals_detail ?? {}, next.residuals_detail ?? {}),
      summary: {
        time_points: times.length, last_time: times.at(-1) ?? null,
        fields: Object.keys(residuals).sort(), iterations_total: a.iterations_total + b.iterations_total,
        courant_max_last: b.courant_max_last ?? a.courant_max_last,
        execution_time_last_s: b.execution_time_last_s ?? a.execution_time_last_s,
        finished_cleanly: a.finished_cleanly || b.finished_cleanly,
        fatal_errors: (a.fatal_errors ?? 0) + (b.fatal_errors ?? 0),
        warnings: (a.warnings ?? 0) + (b.warnings ?? 0),
      },
    },
  };
}
