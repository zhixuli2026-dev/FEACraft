import {
  ACESFilmicToneMapping, Color, DataTexture, EquirectangularReflectionMapping, HalfFloatType,
  LinearFilter, LinearToneMapping, LinearSRGBColorSpace, NeutralToneMapping, NoToneMapping,
  PMREMGenerator, RGBAFormat, SRGBColorSpace,
} from "three";
import type { Scene, Texture, WebGLRenderer } from "three";
import { HDRLoader } from "three/examples/jsm/loaders/HDRLoader.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

export const CAD_STUDIO_ENVIRONMENTS = [
  ["studio", "程序生成影棚"],
  ["studio_small_08", "柔光影棚"],
  ["studio_small_03", "高对比影棚"],
  ["white_studio_05", "明亮中性"],
  ["white_studio_03", "白色柔光箱"],
  ["photo_studio_01", "聚光灯"],
  ["studio_small_09", "摄影棚布光"],
  ["cyclorama_hard_light", "硬光无缝背景"],
  ["canary_wharf", "阴天城市"],
  ["kiara_1_dawn", "户外晨光"],
  ["empty_warehouse_01", "工业仓库"],
  ["san_giuseppe_bridge", "户外桥景"],
] as const;
export type CadStudioPreset = typeof CAD_STUDIO_ENVIRONMENTS[number][0];

export const CAD_STUDIO_TONE = {
  neutral: ["PBR 中性", NeutralToneMapping],
  ACES: ["ACES Filmic", ACESFilmicToneMapping],
  none: ["线性", LinearToneMapping],
} as const;
export const CAD_STUDIO_BACKGROUNDS = {
  environment: "环境", transparent: "透明", gradient: "灰色渐变", "gradient-dark": "深灰渐变",
  white: "白色", grey: "灰色", darkgrey: "深灰色",
} as const;

export interface CadStudioLighting {
  environment: CadStudioPreset;
  highResolution: boolean;
  rotation: number;
  intensity: number;
  exposure: number;
  tone: keyof typeof CAD_STUDIO_TONE;
  background: keyof typeof CAD_STUDIO_BACKGROUNDS;
  shadowIntensity: number;
  shadowSoftness: number;
  aoIntensity: number;
}

export const DEFAULT_CAD_STUDIO: CadStudioLighting = {
  environment: "studio", highResolution: false, rotation: 0, intensity: 1, exposure: 1,
  tone: "neutral", background: "environment", shadowIntensity: 0.5, shadowSoftness: 0.2,
  aoIntensity: 0.5,
};

export interface CadStudioEnvironment {
  preset: CadStudioPreset;
  highResolution: boolean;
  texture: Texture;
  size: [number, number] | null;
  dispose(): void;
}

export function proceduralCadEnvironment(renderer: WebGLRenderer): CadStudioEnvironment {
  const generator = new PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  try {
    const environment = generator.fromScene(room, 0.04);
    return {preset: "studio", highResolution: false, texture: environment.texture, size: null,
      dispose: () => environment.dispose()};
  } finally {
    room.dispose();
    generator.dispose();
  }
}

export async function loadCadEnvironment(preset: Exclude<CadStudioPreset, "studio">, highResolution: boolean,
                                         maxTextureSize: number, signal: AbortSignal): Promise<CadStudioEnvironment> {
  const resolution = highResolution ? "4k" : "1k";
  const width = highResolution ? 4096 : 1024;
  const maxBytes = highResolution ? 40 * 1024 * 1024 : 4 * 1024 * 1024;
  if (width > maxTextureSize) throw new Error("当前显卡不支持所选环境图分辨率。");
  const url = `https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/${resolution}/${preset}_${resolution}.hdr`;
  const response = await fetch(url, {signal, credentials: "omit", referrerPolicy: "no-referrer"}).catch(() => {
    signal.throwIfAborted();
    throw new Error("环境图加载失败，请重试。");
  });
  if (!response.ok) throw new Error("环境图加载失败，请重试。");
  const bytes = await response.arrayBuffer();
  signal.throwIfAborted();
  if (bytes.byteLength > maxBytes) throw new Error("环境图文件大小异常。");
  const data = new HDRLoader().parse(bytes);
  if (data.width !== width || data.height !== width / 2) throw new Error("环境图分辨率异常。");
  const texture = new DataTexture(data.data, data.width, data.height, RGBAFormat, HalfFloatType);
  texture.colorSpace = LinearSRGBColorSpace;
  texture.mapping = EquirectangularReflectionMapping;
  texture.minFilter = texture.magFilter = LinearFilter;
  texture.flipY = true;
  texture.needsUpdate = true;
  return {preset, highResolution, texture, size: [width, width / 2], dispose: () => texture.dispose()};
}

const backgrounds = {white: 0xffffff, grey: 0x93969c, darkgrey: 0x25282e};

export function cadStudioGradient(dark: boolean): Texture {
  const pixels = new Uint8Array(4 * 128);
  for (let i = 0; i < 128; i++) {
    const channel = Math.round((dark ? 19 : 99) + i * (dark ? 0.4 : 0.8));
    pixels.set([channel, channel, channel, 255], i * 4);
  }
  const texture = new DataTexture(pixels, 1, 128);
  texture.colorSpace = SRGBColorSpace;
  texture.minFilter = texture.magFilter = LinearFilter;
  texture.needsUpdate = true;
  return texture;
}

export function applyCadStudioLighting(renderer: WebGLRenderer, scene: Scene, enabled: boolean,
                                       settings: CadStudioLighting, environment: CadStudioEnvironment | null,
                                       gradients: {gradient: Texture; "gradient-dark": Texture},
                                       darkTheme: boolean) {
  if (!enabled) {
    scene.environment = null;
    scene.background = new Color(darkTheme ? 0x0f1115 : 0xf4f6fa);
    renderer.toneMapping = NoToneMapping;
    renderer.toneMappingExposure = 1;
    return;
  }
  const rotation = settings.rotation * Math.PI / 180;
  scene.environment = environment?.texture ?? null;
  scene.environmentIntensity = settings.intensity;
  scene.environmentRotation.set(0, rotation, 0);
  scene.backgroundRotation.set(0, rotation, 0);
  scene.backgroundIntensity = settings.intensity;
  renderer.toneMapping = CAD_STUDIO_TONE[settings.tone][1];
  renderer.toneMappingExposure = settings.exposure;
  if (settings.background === "environment") scene.background = environment?.texture ?? null;
  else if (settings.background === "transparent") scene.background = null;
  else if (settings.background === "gradient" || settings.background === "gradient-dark") {
    scene.background = gradients[settings.background];
    scene.backgroundIntensity = 1;
  } else scene.background = new Color(backgrounds[settings.background]);
}
