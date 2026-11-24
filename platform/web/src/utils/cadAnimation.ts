import { Quaternion } from "three";

export const CAD_ANIMATION_SCHEMA = "vibeflow.cadquery.animation.v1";
export const CAD_ANIMATION_ACTIONS = ["t", "tx", "ty", "tz", "q", "rx", "ry", "rz"] as const;
export type CadAnimationAction = typeof CAD_ANIMATION_ACTIONS[number];
export type CadAnimationValue = number | number[];

export interface CadAnimationTrack {
  selector: string;
  objectId?: string;
  action: CadAnimationAction;
  times: number[];
  values: CadAnimationValue[];
}

export interface CadAnimationDocument {
  schema: typeof CAD_ANIMATION_SCHEMA;
  speed: number;
  repeat: boolean;
  duration: number;
  tracks: CadAnimationTrack[];
}

export interface CadAnimationSample {
  selector: string;
  action: CadAnimationAction;
  value: CadAnimationValue;
}

export interface CadAnimationTarget { key: string; name: string }
export interface BoundCadAnimationTrack { key: string; track: CadAnimationTrack }

type RecordValue = Record<string, unknown>;

function asRecord(value: unknown, label: string): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} 必须是对象`);
  return value as RecordValue;
}

function finite(value: unknown, label: string): number {
  if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) {
    throw new Error(`${label} 必须是有限数值`);
  }
  const result = Number(value);
  if (!Number.isFinite(result)) throw new Error(`${label} 必须是有限数值`);
  return result;
}

function dimension(action: CadAnimationAction): number {
  if (action === "t") return 3;
  if (action === "q") return 4;
  return 1;
}

function normalizeTrack(value: unknown): CadAnimationTrack {
  const item = asRecord(value, "轨道");
  const selector = item.selector;
  if (typeof selector !== "string" || selector.length < 1 || selector.length > 120) {
    throw new Error("轨道 selector 须为 1–120 字符");
  }
  const objectId = item.objectId;
  if (objectId !== undefined && (typeof objectId !== "string" || !objectId.trim() || objectId.length > 800)) {
    throw new Error("轨道 objectId 无效");
  }
  const action = item.action as CadAnimationAction;
  if (!CAD_ANIMATION_ACTIONS.includes(action)) throw new Error(`未知动画 action: ${String(action)}`);
  if (!Array.isArray(item.times) || item.times.length < 2 || item.times.length > 64) {
    throw new Error("轨道 times 须为 2–64 项");
  }
  if (!Array.isArray(item.values) || item.values.length !== item.times.length) {
    throw new Error("轨道 times 与 values 必须等长");
  }
  let previous = -Infinity;
  const times = item.times.map(value => {
    const time = finite(value, "关键帧时间");
    if (time < 0 || time <= previous) throw new Error("关键帧时间必须非负且严格递增");
    previous = time;
    return time;
  });
  const width = dimension(action);
  const values = item.values.map(value => {
    if (width === 1) return finite(value, `${action} value`);
    if (!Array.isArray(value) || value.length !== width) throw new Error(`${action} value 维度错误`);
    const vector = value.map(component => finite(component, `${action} value`));
    if (action === "q") {
      const norm = Math.hypot(...vector);
      if (norm <= 1e-12) throw new Error("四元数不能为零");
      return vector.map(component => component / norm);
    }
    return vector;
  });
  return {selector, ...(objectId === undefined ? {} : {objectId}), action, times, values};
}

export function normalizeCadAnimation(value: unknown): CadAnimationDocument {
  const input = typeof value === "string" ? JSON.parse(value) : value;
  const document = asRecord(input, "动画文档");
  if (document.schema === "vibeflow.animation.camera.v1") {
    throw new Error("这是可视化相机轨道，请在可视化动画面板应用。");
  }
  if (document.schema !== CAD_ANIMATION_SCHEMA) throw new Error(`动画 schema 须为 ${CAD_ANIMATION_SCHEMA}`);
  const speed = finite(document.speed ?? 1, "speed");
  if (speed <= 0 || speed > 100) throw new Error("speed 须在 (0,100]");
  if (!Array.isArray(document.tracks) || document.tracks.length < 1 || document.tracks.length > 64) {
    throw new Error("动画 tracks 须为 1–64 项");
  }
  const tracks = document.tracks.map(normalizeTrack);
  const duration = Math.max(...tracks.map(track => track.times[track.times.length - 1]));
  if (!Number.isFinite(duration) || duration <= 0) throw new Error("动画 duration 必须大于零");
  const repeat = document.repeat ?? document.loop ?? true;
  if (typeof repeat !== "boolean" && repeat !== "true" && repeat !== "false") throw new Error("repeat 须为布尔值");
  return {schema: CAD_ANIMATION_SCHEMA, speed, repeat: repeat === true || repeat === "true", duration, tracks};
}

export function sampleCadAnimationTrack(track: CadAnimationTrack, time: number): CadAnimationValue {
  const times = track.times;
  const local = Math.min(Math.max(time, times[0]), times[times.length - 1]);
  let right = times.findIndex(value => value >= local);
  if (right < 0) right = times.length - 1;
  const left = Math.max(0, right - 1);
  const span = times[right] - times[left];
  const alpha = span === 0 ? 0 : (local - times[left]) / span;
  const a = track.values[left], b = track.values[right];
  if (track.action === "q") {
    const first = a as number[], second = b as number[];
    const qa = new Quaternion(first[0], first[1], first[2], first[3]);
    const qb = new Quaternion(second[0], second[1], second[2], second[3]);
    qa.slerp(qb, alpha);
    return qa.toArray();
  }
  if (track.action === "t") {
    return (a as number[]).map((value, index) => value + ((b as number[])[index] - value) * alpha);
  }
  return (a as number) + ((b as number) - (a as number)) * alpha;
}

export function sampleCadAnimation(value: unknown, timeValue: number): CadAnimationSample[] {
  const document = normalizeCadAnimation(value);
  const time = Math.min(Math.max(finite(timeValue, "采样时间"), 0), document.duration);
  return document.tracks.map(track => ({selector: track.selector, action: track.action, value: sampleCadAnimationTrack(track, time)}));
}

export function canonicalCadAnimationJson(value: unknown): string {
  return JSON.stringify(normalizeCadAnimation(value), null, 2) + "\n";
}

export function cadSelectorMatches(selector: string, name: string): boolean {
  const normalize = (value: string) => value.replace(/^[\/|]+/, "").replace(/[\/|]/g, "_");
  return normalize(selector) === normalize(name);
}

export function bindCadAnimationTracks(document: CadAnimationDocument,
  objects: readonly CadAnimationTarget[]): BoundCadAnimationTrack[] {
  return document.tracks.map(track => {
    const {objectId, selector} = track;
    if (objectId !== undefined) {
      const target = objects.find(object => object.key === objectId);
      if (!target) throw new Error(`当前场景找不到轨道实例：${selector}`);
      return {key: target.key, track};
    }
    const exact = objects.filter(object => object.name === selector);
    const unprefixed = selector.replace(/^[\/|]+/, "");
    const legacyExact = exact.length || unprefixed === selector
      ? exact : objects.filter(object => object.name === unprefixed);
    const targets = legacyExact.length ? legacyExact : objects.filter(object => cadSelectorMatches(selector, object.name));
    if (targets.length !== 1) throw new Error(targets.length
      ? `选择器匹配多个对象：${selector}。请从对象属性添加轨道。`
      : `当前场景找不到轨道对象：${selector}`);
    return {key: targets[0].key, track};
  });
}
