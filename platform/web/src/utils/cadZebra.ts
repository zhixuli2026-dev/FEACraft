export const ZEBRA_TEXTURE_HEIGHT = 4096;
export const ZEBRA_SCHEMES = ["blackwhite", "colorful", "grayscale", "custom"] as const;
export const ZEBRA_MAPPINGS = ["reflection", "normal"] as const;
export type ZebraScheme = typeof ZEBRA_SCHEMES[number];
export type ZebraMapping = typeof ZEBRA_MAPPINGS[number];
export type ZebraColors = readonly [string, string];
export const DEFAULT_ZEBRA_COLORS: ZebraColors = ["#151922", "#f2c45d"];

export function normalizeZebraScheme(value: unknown): ZebraScheme {
  if (!ZEBRA_SCHEMES.includes(value as ZebraScheme)) throw new Error(`未知 Zebra color_scheme: ${String(value)}`);
  return value as ZebraScheme;
}

export function normalizeZebraMapping(value: unknown): ZebraMapping {
  if (!ZEBRA_MAPPINGS.includes(value as ZebraMapping)) throw new Error(`未知 Zebra mapping_mode: ${String(value)}`);
  return value as ZebraMapping;
}

export function normalizeZebraAngle(value: unknown): number {
  if (value === null || value === "" || typeof value === "boolean") throw new Error("Zebra 角度必须是数值");
  const angle = Number(value);
  if (!Number.isFinite(angle) || angle < 0 || angle > 360) throw new Error("Zebra 角度须在 [0,360]");
  return angle;
}

export function zebraDirectionVector(angle: number): number[] {
  const radians = normalizeZebraAngle(angle) * Math.PI / 180;
  return [Math.cos(radians), Math.sin(radians), 0];
}

function hslToRgb(hue: number, saturation: number, lightness: number): number[] {
  const h = ((hue % 360) + 360) % 360 / 60;
  const c = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const x = c * (1 - Math.abs(h % 2 - 1));
  const m = lightness - c / 2;
  const [r, g, b] = h < 1 ? [c, x, 0] : h < 2 ? [x, c, 0] : h < 3 ? [0, c, x]
    : h < 4 ? [0, x, c] : h < 5 ? [x, 0, c] : [c, 0, x];
  return [r, g, b].map(component => Math.round((component + m) * 255));
}

function customRgb(color: string): number[] {
  if (!/^#[0-9a-f]{6}$/i.test(color)) throw new Error("Zebra 颜色须为 #RRGGBB");
  return [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16));
}

export function buildZebraPixels(count: number, scheme: ZebraScheme, colors = DEFAULT_ZEBRA_COLORS): Uint8Array {
  if (!Number.isInteger(count) || count < 2 || count > 50) throw new Error("Zebra 条纹数须为 2–50 的整数");
  normalizeZebraScheme(scheme);
  const data = new Uint8Array(ZEBRA_TEXTURE_HEIGHT * 4);
  const customColors = scheme === "custom" ? colors.map(customRgb) : [];
  const odd = count % 2 === 1;
  const units = odd ? count - 1 : count;
  const stripeHeight = ZEBRA_TEXTURE_HEIGHT / units;
  let currentY = 0;
  for (let index = 0; index < count; index++) {
    let first: number[], second: number[];
    if (scheme === "blackwhite") {
      first = [0, 0, 0]; second = [255, 255, 255];
    } else if (scheme === "grayscale") {
      first = [210, 210, 210]; second = [70, 70, 70];
    } else if (scheme === "custom") {
      [first, second] = customColors;
    } else {
      const hue = odd && index === count - 1 ? 0 : index / units * 360;
      first = hslToRgb(hue, 1, 0.5);
      second = hslToRgb(hue + 180, 1, 0.5);
    }
    const height = odd && (index === 0 || index === count - 1) ? stripeHeight / 2 : stripeHeight;
    const start = Math.round(currentY);
    const end = Math.min(ZEBRA_TEXTURE_HEIGHT, Math.round(currentY + height));
    const color = index % 2 === 0 ? first : second;
    for (let y = start; y < end; y++) {
      data[y * 4] = color[0];
      data[y * 4 + 1] = color[1];
      data[y * 4 + 2] = color[2];
      data[y * 4 + 3] = 255;
    }
    currentY += height;
  }
  return data;
}
