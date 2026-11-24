export type CadControl = "orbit" | "trackball";
export type CadViewMode = "single" | "quad";
export type CadViewPreset = "iso" | "x" | "y" | "z";
export type CadGridPlane = "xz" | "xy" | "yz";
export type CadGrid = Record<CadGridPlane, boolean>;
export const DEFAULT_CAD_GRID: CadGrid = {xz: true, xy: false, yz: false};

export const CAD_VIEW_PRESETS: Record<CadViewPreset, string> = {iso: "等轴测", x: "+X", y: "+Y", z: "+Z"};
export const CAD_VIEW_DIRECTIONS: Record<CadViewPreset, [number, number, number]> = {
  iso: [1, 0.7, 1], x: [1, 0, 0], y: [0, 1, 0], z: [0, 0, 1],
};

export interface CadMaterialSettings {
  ambientIntensity: number;
  directIntensity: number;
  metalness: number;
  roughness: number;
}

export interface CadViewport {
  x: number;
  y: number;
  width: number;
  height: number;
  preset: CadViewPreset;
}

export interface CadSurfaceSettings {
  clearcoat: number;
  clearcoatRoughness: number;
  transmission: number;
  ior: number;
  sheen: number;
}

export const CAD_SURFACES: Record<string, {label: string; values: CadSurfaceSettings}> = {
  standard: {label: "标准", values: {clearcoat: 0, clearcoatRoughness: 0, transmission: 0, ior: 1.5, sheen: 0}},
  coated: {label: "亮面清漆", values: {clearcoat: 0.9, clearcoatRoughness: 0.12, transmission: 0, ior: 1.5, sheen: 0}},
  glass: {label: "透明玻璃", values: {clearcoat: 0, clearcoatRoughness: 0, transmission: 0.96, ior: 1.46, sheen: 0}},
  satin: {label: "缎面光泽", values: {clearcoat: 0.2, clearcoatRoughness: 0.35, transmission: 0, ior: 1.48, sheen: 0.9}},
};

export const CAD_SURFACE_FIELDS: Record<keyof CadSurfaceSettings, {label: string; min: number; max: number}> = {
  clearcoat: {label: "清漆", min: 0, max: 1},
  clearcoatRoughness: {label: "清漆粗糙度", min: 0, max: 1},
  transmission: {label: "透射", min: 0, max: 1},
  ior: {label: "折射率", min: 1, max: 2.33},
  sheen: {label: "缎面光泽", min: 0, max: 1},
};

function finite(value: unknown, label: string): number {
  if (value === null || value === "" || typeof value === "boolean") {
    throw new Error(`${label} 必须是有限数值`);
  }
  const result = Number(value);
  if (!Number.isFinite(result)) throw new Error(`${label} 必须是有限数值`);
  return result;
}

export function cadOrthoSpan(size: readonly number[], aspect: number, margin = 1.2): number {
  if (size.length !== 3) throw new Error("正交 size 必须是三维向量");
  const dimensions = size.map((value) => Math.abs(finite(value, "正交 size")));
  const widthAspect = finite(aspect, "aspect");
  const safeMargin = finite(margin, "margin");
  if (widthAspect <= 0 || safeMargin <= 0) throw new Error("aspect/margin 参数无效");
  const diameter = Math.max(Math.hypot(...dimensions), 1e-6);
  return diameter * safeMargin / Math.min(1, widthAspect);
}

export function normalizeCadControl(value: unknown): CadControl {
  return value === "trackball" ? "trackball" : "orbit";
}

export function normalizeCadViewMode(value: unknown): CadViewMode {
  return value === "quad" ? "quad" : "single";
}

export function normalizeCadMaterial(value: unknown): CadMaterialSettings {
  if (value !== null && value !== undefined && (typeof value !== "object" || Array.isArray(value))) {
    throw new Error("材质设置必须是对象");
  }
  const input = (value ?? {}) as Record<string, unknown>;
  const ambientIntensity = finite(input.ambientIntensity ?? 1, "ambientIntensity");
  const directIntensity = finite(input.directIntensity ?? 1.1, "directIntensity");
  const metalness = finite(input.metalness ?? 0.3, "metalness");
  const roughness = finite(input.roughness ?? 0.65, "roughness");
  if (ambientIntensity < 0 || ambientIntensity > 4 || directIntensity < 0 || directIntensity > 4) {
    throw new Error("灯光强度须在 [0,4]");
  }
  if (metalness < 0 || metalness > 1 || roughness < 0 || roughness > 1) {
    throw new Error("metalness/roughness 须在 [0,1]");
  }
  return {ambientIntensity, directIntensity, metalness, roughness};
}

export function normalizeCadSurface(settings: CadSurfaceSettings): CadSurfaceSettings {
  const result = {...settings};
  for (const key of Object.keys(CAD_SURFACE_FIELDS) as (keyof CadSurfaceSettings)[]) {
    const {label, min, max} = CAD_SURFACE_FIELDS[key];
    const value = finite(settings[key], label);
    if (value < min || value > max) throw new Error(`${label} 须在 [${min},${max}]`);
    result[key] = value;
  }
  return result;
}

export function cadQuadViewports(width: number, height: number): CadViewport[] {
  const safeWidth = finite(width, "width");
  const safeHeight = finite(height, "height");
  if (safeWidth < 2 || safeHeight < 2) throw new Error("四宫格视口宽高必须至少为 2");
  const left = Math.floor(safeWidth / 2);
  const right = safeWidth - left;
  const bottom = Math.floor(safeHeight / 2);
  const top = safeHeight - bottom;
  return [
    {x: 0, y: bottom, width: left, height: top, preset: "iso"},
    {x: left, y: bottom, width: right, height: top, preset: "x"},
    {x: 0, y: 0, width: left, height: bottom, preset: "y"},
    {x: left, y: 0, width: right, height: bottom, preset: "z"},
  ];
}
