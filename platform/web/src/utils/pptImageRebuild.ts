import { createWorker, OEM, PSM } from "tesseract.js";
import workerPath from "tesseract.js/dist/worker.min.js?url";
import corePath from "tesseract.js-core/tesseract-core-lstm.wasm.js?url";

type Bounds = { x: number; y: number; width: number; height: number };
export type PptRebuild = {
  background: string;
  aspect_ratio: number;
  text_regions: (Bounds & { text: string; color: string; size: number })[];
  shapes: (Bounds & { preset: "rect" | "ellipse"; fill: string; stroke: string })[];
};

const rgb = (pixels: Uint8ClampedArray, index: number) => "#" +
  Array.from(pixels.subarray(index * 4, index * 4 + 3), (value) => value.toString(16).padStart(2, "0")).join("");

function colorKey(pixels: Uint8ClampedArray, index: number): number {
  const offset = index * 4;
  return 1 + ((pixels[offset] >> 4) << 8) + ((pixels[offset + 1] >> 4) << 4) + (pixels[offset + 2] >> 4);
}

function findShapes({ data, width, height }: ImageData, background: number): PptRebuild["shapes"] {
  const total = width * height;
  const labels = new Uint16Array(total);
  const queue = new Int32Array(total);
  for (let index = 0; index < total; index++) {
    const key = colorKey(data, index);
    if (key !== background) labels[index] = key;
  }
  const candidates: (PptRebuild["shapes"][number] & { area: number })[] = [];
  for (let index = 0; index < total; index++) {
    const key = labels[index];
    if (key === 0) continue;
    let end = 1;
    function enqueue(neighbor: number) {
      if (labels[neighbor] !== key) return;
      labels[neighbor] = 0;
      queue[end++] = neighbor;
    }
    labels[index] = 0;
    queue[0] = index;
    let left = width, right = 0, top = height, bottom = 0;
    for (let head = 0; head < end; head++) {
      const pixel = queue[head], x = pixel % width, y = Math.floor(pixel / width);
      left = Math.min(left, x); right = Math.max(right, x);
      top = Math.min(top, y); bottom = Math.max(bottom, y);
      if (x > 0) enqueue(pixel - 1);
      if (x < width - 1) enqueue(pixel + 1);
      if (y > 0) enqueue(pixel - width);
      if (y < height - 1) enqueue(pixel + width);
    }
    const w = right - left + 1, h = bottom - top + 1;
    if (w < 18 || h < 18 || end < total * 0.001) continue;
    const coverage = end / (w * h);
    const corners = [top * width + left, top * width + right, bottom * width + left, bottom * width + right];
    const filledCorners = corners.filter((corner) => colorKey(data, corner) === key).length;
    const preset = coverage > 0.9 || (coverage > 0.66 && filledCorners >= 3) ? "rect"
      : coverage >= 0.69 && coverage <= 0.85 && filledCorners === 0 ? "ellipse" : null;
    if (!preset) continue;
    const fill = rgb(data, index);
    candidates.push({ preset, x: left / width, y: top / height, width: w / width,
      height: h / height, fill, stroke: fill, area: end });
  }
  return candidates.sort((a, b) => b.area - a.area).slice(0, 40).map(({ area: _area, ...shape }) => shape);
}

export async function recognizePptImage(file: Blob, language: "eng" | "chi_sim", signal: AbortSignal): Promise<PptRebuild> {
  if (!file.size || file.size > 16 * 1024 * 1024) throw new Error("请选择 16 MiB 以内的图片");
  const bitmap = await createImageBitmap(file);
  const ratio = bitmap.width / bitmap.height;
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d", { willReadFrequently: true })!;
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  if (ratio < 0.25 || ratio > 4) throw new Error("图片宽高比超出支持范围");
  signal.throwIfAborted();

  const worker = await createWorker(language, OEM.LSTM_ONLY, {
    workerPath, corePath, langPath: new URL("/ppt-ocr", location.origin).href,
    workerBlobURL: false,
  });
  let cancel!: () => void;
  const cancelled = new Promise<never>((_resolve, reject) => { cancel = () => reject(signal.reason); });
  try {
    signal.throwIfAborted();
    signal.addEventListener("abort", cancel, { once: true });
    const { data } = await Promise.race([cancelled, (async () => {
      await worker.setParameters({ tessedit_pageseg_mode: PSM.SPARSE_TEXT });
      return worker.recognize(canvas, {}, { blocks: true });
    })()]);
    signal.throwIfAborted();
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const { width, height } = pixels;
    const corners = [0, width - 1, width * (height - 1), width * height - 1];
    const background = corners.reduce((best, point) => {
      const count = (candidate: number) => corners.filter((p) => colorKey(pixels.data, candidate) === colorKey(pixels.data, p)).length;
      return count(point) > count(best) ? point : best;
    });
    const text_regions: PptRebuild["text_regions"] = [];
    for (const block of data.blocks ?? []) {
      for (const paragraph of block.paragraphs) {
        for (const line of paragraph.lines) {
          const text = line.text.trim().slice(0, 800);
          const { x0, y0, x1, y1 } = line.bbox;
          const lineHeight = y1 - y0;
          if (!text || line.confidence < 20 || lineHeight < 4 || x1 - x0 < 4) continue;
          const top = Math.max(0, y0 - lineHeight * 0.22);
          let darkest = y0 * width + x0, lum = 256;
          for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
            const point = y * width + x, offset = point * 4;
            const level = 0.21 * pixels.data[offset] + 0.72 * pixels.data[offset + 1] + 0.07 * pixels.data[offset + 2];
            if (level < lum) { lum = level; darkest = point; }
          }
          text_regions.push({ text, color: rgb(pixels.data, darkest), x: x0 / width, y: top / height,
            width: (Math.min(width, x1 + 6) - x0) / width,
            height: (Math.min(height, y1 + lineHeight * 0.32) - top) / height,
            size: Math.max(0.0025, Math.min(0.75, (lineHeight * 1.12) / height)) });
          if (text_regions.length >= 120) break;
        }
        if (text_regions.length >= 120) break;
      }
      if (text_regions.length >= 120) break;
    }
    return { background: rgb(pixels.data, background), aspect_ratio: ratio, text_regions,
      shapes: findShapes(pixels, colorKey(pixels.data, background)) };
  } finally {
    signal.removeEventListener("abort", cancel);
    await worker.terminate();
  }
}
