export interface PdfRangeOptions {
  url: string;
  token: string;
  length: number;
  version: string;
  onRequest?: (begin: number, end: number) => void;
  onError?: (error: Error) => void;
}

/** PDF.js 2.16 PDFDataRangeTransport 的 Bearer + 单段 Range 实现。 */
export function createPdfRangeTransport(lib: any, options: PdfRangeOptions) {
  let loadingTask: any = null;
  let aborted = false;
  let failure: Error | null = null;
  const controllers = new Set<AbortController>();
  const destroy = () => {
    if (loadingTask) void Promise.resolve(loadingTask.destroy()).catch(() => {});
  };
  const fail = (error: Error) => {
    if (aborted) return;
    failure = error;
    aborted = true;
    controllers.forEach(controller => controller.abort());
    controllers.clear();
    options.onError?.(error);
    destroy();
  };

  class AuthenticatedRangeTransport extends lib.PDFDataRangeTransport {
    constructor() {
      super(options.length, new Uint8Array());
    }

    requestDataRange(begin: number, end: number) {
      if (aborted) return;
      if (!/^[0-9a-f]{64}$/.test(options.version)) {
        fail(new Error("PDF 缺少有效版本信息，请重新读取"));
        return;
      }
      if (!Number.isSafeInteger(begin) || !Number.isSafeInteger(end) || begin < 0
          || end <= begin || end > options.length) {
        fail(new Error("PDF 请求范围无效"));
        return;
      }
      const controller = new AbortController();
      controllers.add(controller);
      options.onRequest?.(begin, end);
      const headers: Record<string, string> = { Range: `bytes=${begin}-${end - 1}`,
        "X-Vibeflow-File-Version": options.version };
      if (options.token) headers.Authorization = `Bearer ${options.token}`;
      fetch(options.url, { headers, signal: controller.signal })
        .then(async (response) => {
          if (response.status === 409) throw new Error("PDF 文件已变化，请重新读取");
          if (response.status !== 206) throw new Error(`PDF Range 请求失败 (${response.status})`);
          if (response.headers.get("X-Vibeflow-File-Version") !== options.version) {
            throw new Error("PDF 版本校验失败，请重新读取");
          }
          if (response.headers.get("Content-Range") !== `bytes ${begin}-${end - 1}/${options.length}`
              || response.headers.get("Content-Length") !== String(end - begin)) {
            throw new Error("PDF 响应范围不符，请重新读取");
          }
          const data = await response.arrayBuffer();
          if (data.byteLength !== end - begin) {
            throw new Error(`PDF Range 长度不符：期望 ${end - begin}，实际 ${data.byteLength}`);
          }
          controllers.delete(controller);
          if (!aborted) this.onDataRange(begin, new Uint8Array(data));
        })
        .catch((cause) => {
          controllers.delete(controller);
          if (aborted || cause?.name === "AbortError") return;
          const error = cause instanceof Error ? cause : new Error(String(cause));
          fail(error);
        });
    }

    abort() {
      aborted = true;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    }
  }

  const transport = new AuthenticatedRangeTransport();
  return {
    transport,
    setLoadingTask(task: any) { loadingTask = task; if (failure) destroy(); },
    abort() { transport.abort(); },
  };
}
