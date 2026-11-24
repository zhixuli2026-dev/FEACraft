export interface CropPoint { x: number; y: number }
export interface CropRect { x: number; y: number; width: number; height: number }

/** 将拖动坐标规范化为视频像素裁剪框； clamp 到视频内且至少 1px。 */
export function normalizeCrop(start: CropPoint, end: CropPoint,
                              width: number, height: number): CropRect {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("视频尺寸无效");
  }
  const clamp = (value: number, max: number) => Math.max(0, Math.min(max, value));
  const x1 = clamp(Math.floor(Math.min(start.x, end.x)), width - 1);
  const y1 = clamp(Math.floor(Math.min(start.y, end.y)), height - 1);
  const x2 = clamp(Math.ceil(Math.max(start.x, end.x)), width);
  const y2 = clamp(Math.ceil(Math.max(start.y, end.y)), height);
  return { x: x1, y: y1, width: Math.max(1, x2 - x1), height: Math.max(1, y2 - y1) };
}
