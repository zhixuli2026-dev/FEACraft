import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { insertAtCursor, revealLine } from "../shell/editorRef";
import { activatePanel } from "../shell/dock";
import { useStore } from "../store";
import DocumentPanel from "./DocumentPanel";
import ReportHistory from "./ReportHistory";
import PdfSource from "./PdfSource";
import type { FrozenPdfSource } from "./PdfSource";
import { latexMainForPdf, pdfRequest, selectPdf } from "../utils/pdfPublication";
import type { PdfSelection } from "../utils/pdfPublication";
import { TablePanel } from "./TablePanel";
import { createPdfRangeTransport } from "../utils/pdfRange";
import { pdfHighlightRects, pdfPageSearch, pdfSearchQuery, readPdfText, type PdfTextItem } from "../utils/pdfTextSearch";
import "./PdfReader.css";
import { TexoReceiptStore } from "../utils/texoCache";
import { normalizeCrop, type CropPoint } from "../utils/screenCrop";
import { publicationLabel, usePublicationStatus } from '../utils/publicationStatus';
import { publicationChannel, readPdfView, savePdfView } from '../utils/viewSelection';
import { readPublishedSourceHint, savePublishedSourceHint, type PublishedSourceHint } from '../utils/viewSelection';

/** 图片查看器（Bearer Blob + 缩放、滚轮、拖拽平移和复位） */
type ImageRead = { key: string; status: "decoding" | "ready"; url: string }
  | { key: string; status: "error"; message: string };

export function ImagePanel() {
  const { activeFile, currentCase, user } = useStore();
  const [read, setRead] = useState<ImageRead | null>(null);
  const [reload, setReload] = useState(0);
  const [scale, setScale] = useState(1);
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const [rotation, setRotation] = useState(0);
  const [nativeFullscreen, setNativeFullscreen] = useState(false);
  const [fullscreenFallback, setFullscreenFallback] = useState(false);
  const [panning, setPanning] = useState(false);
  const viewerRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointer: number; x: number; y: number; px: number; py: number } | null>(null);
  const caseId = currentCase?.id;
  const key = JSON.stringify([user?.id, caseId, activeFile, reload]);
  const current = read?.key === key ? read : null;
  const ready = current?.status === "ready";
  const isFullscreen = nativeFullscreen || fullscreenFallback;
  const quarterTurn = rotation % 180 !== 0;

  useEffect(() => {
    if (!activeFile || !caseId) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    drag.current = null;
    setPanning(false);
    setScale(1);
    setPosition({ x: 0, y: 0 });
    setRotation(0);
    void (async () => {
      try {
        const token = localStorage.getItem("vf_token");
        const res = await fetch(`/api/v1/cases/${caseId}/raw?path=${encodeURIComponent(activeFile)}`,
          { signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!res.ok) throw new Error(`图片读取失败 (${res.status})`);
        const blob = await res.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setRead({ key, status: "decoding", url: objectUrl });
      } catch (cause) {
        if (!controller.signal.aborted) setRead({ key, status: "error",
          message: cause instanceof Error ? cause.message : String(cause) });
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [activeFile, caseId, key]);
  useEffect(() => {
    const changed = () => setNativeFullscreen(document.fullscreenElement === viewerRef.current && !!viewerRef.current);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  useEffect(() => {
    if (!fullscreenFallback) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFullscreenFallback(false);
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [fullscreenFallback]);

  const zoom = useCallback((delta: number) => setScale(value => Math.min(8, Math.max(0.2, value + delta))), []);
  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface || !ready) return;
    const wheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      event.preventDefault();
      zoom(event.deltaY < 0 ? 0.25 : -0.25);
    };
    surface.addEventListener("wheel", wheel, { passive: false });
    return () => surface.removeEventListener("wheel", wheel);
  }, [fullscreenFallback, ready, zoom]);

  const reset = () => { setScale(1); setPosition({ x: 0, y: 0 }); setRotation(0); };
  const stopDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current = null;
    setPanning(false);
  };
  const toggleFullscreen = async () => {
    if (fullscreenFallback) {
      setFullscreenFallback(false);
      return;
    }
    const viewer = viewerRef.current;
    if (!viewer) return;
    if (document.fullscreenElement === viewer) {
      await document.exitFullscreen();
      return;
    }
    if (document.fullscreenElement) return;
    try {
      await viewer.requestFullscreen();
    } catch {
      setFullscreenFallback(true);
    }
  };
  if (!activeFile || !caseId) return <div style={s.hint}>未选择文件</div>;
  const viewer = (
    <div ref={viewerRef} data-testid="image-viewer" data-file={activeFile}
         data-fullscreen={isFullscreen ? "true" : "false"}
         style={{ display: "flex", flexDirection: "column", background: "#0b0d12",
                  ...(fullscreenFallback ? { position: "fixed", inset: 0, zIndex: 9999 } : { height: "100%" }) }}>
      <div style={s.bar}>
        <button data-testid="image-zoom-out" aria-label="缩小图片" style={s.miniBtn} disabled={!ready} onClick={() => zoom(-0.25)}>−</button>
        <span data-testid="image-scale" style={{ color: "#9ab", minWidth: 44, textAlign: "center" }}>{Math.round(scale * 100)}%</span>
        <button data-testid="image-zoom-in" aria-label="放大图片" style={s.miniBtn} disabled={!ready} onClick={() => zoom(0.25)}>+</button>
        <button data-testid="image-rotate" style={s.miniBtn} disabled={!ready}
                onClick={() => setRotation((value) => (value + 90) % 360)}>旋转 {rotation}°</button>
        <button data-testid="image-fullscreen" style={s.miniBtn} onClick={toggleFullscreen}>
          {isFullscreen ? "退出全屏" : "全屏"}
        </button>
        <button data-testid="image-reset" style={s.miniBtn} disabled={!ready} onClick={reset}>适应 / 复位</button>
        <span style={{ color: "#a7b4c7", fontSize: 11 }}>滚轮缩放 · 拖拽平移 · 双击复位</span>
      </div>
      <div ref={surfaceRef} data-testid="image-canvas" aria-busy={!current || current.status === "decoding"}
           style={{ flex: 1, minHeight: 0, containerType: "size", overflow: "hidden", display: "flex", alignItems: "center",
                    justifyContent: "center", touchAction: ready ? "none" : undefined,
                    cursor: panning ? "grabbing" : ready ? "grab" : undefined, userSelect: "none" }}
           onDoubleClick={reset}
           onPointerDown={(event) => {
             if (!ready || event.button !== 0 || drag.current) return;
             drag.current = { pointer: event.pointerId, x: event.clientX, y: event.clientY, px: position.x, py: position.y };
             event.currentTarget.setPointerCapture(event.pointerId);
             setPanning(true);
           }}
           onPointerMove={(event) => {
             if (drag.current?.pointer !== event.pointerId) return;
             setPosition({ x: drag.current.px + event.clientX - drag.current.x,
                           y: drag.current.py + event.clientY - drag.current.y });
           }}
           onPointerUp={stopDrag} onPointerCancel={stopDrag} onLostPointerCapture={stopDrag}>
        {current?.status === "error" && <div style={{ textAlign: "center", padding: 16 }}>
          <p data-testid="image-error" role="alert" style={{ color: "#d7dce5", marginTop: 0 }}>{current.message}</p>
          <button style={s.miniBtn} onClick={() => setReload(value => value + 1)}>重新读取</button>
        </div>}
        {(!current || current.status === "decoding") &&
          <span role="status" style={{ color: "#d7dce5", position: "absolute" }}>加载中…</span>}
        {current && current.status !== "error" && (
          <img key={current.url} data-testid="image-img" src={current.url} alt={activeFile} draggable={false}
               onLoad={() => setRead(previous => previous?.key === key && previous.status === "decoding"
                 ? { ...previous, status: "ready" } : previous)}
               onError={() => setRead(previous => previous?.key === key
                 ? { key, status: "error", message: "图片解码失败。文件修复后可重新读取。" } : previous)}
               style={{ maxWidth: quarterTurn ? "100cqh" : "100cqw", maxHeight: quarterTurn ? "100cqw" : "100cqh",
                        flexShrink: 0, objectFit: "contain", visibility: ready ? "visible" : "hidden",
                        transform: `translate(${position.x}px, ${position.y}px) rotate(${rotation}deg) scale(${scale})` }} />
        )}
      </div>
    </div>
  );
  return fullscreenFallback ? createPortal(viewer, document.body) : viewer;
}

/** pdf.js 模块级加载器（promise 单例 + 轮询等 UMD 全局挂载，免疫 StrictMode 双挂载时序） */
let pdfjsPromise: Promise<any> | null = null;
function loadPdfjs(): Promise<any> {
  if (!pdfjsPromise) {
    pdfjsPromise = new Promise<any>((resolve, reject) => {
      const w = window as any;
      const finish = () => {
        const check = () => {
          if (w.pdfjsLib) {
            w.pdfjsLib.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.js";
            resolve(w.pdfjsLib);
          } else {
            setTimeout(check, 50);
          }
        };
        check();
      };
      if (w.pdfjsLib) { finish(); return; }
      const sc = document.createElement("script");
      sc.src = "/pdfjs/pdf.min.js";
      sc.onload = finish;
      sc.onerror = () => reject(new Error("pdf.js 脚本加载失败"));
      document.head.appendChild(sc);
    });
  }
  return pdfjsPromise;
}

interface PdfOutlineEntry {
  title: string;
  depth: number;
  dest: any;
  url?: string;
}

interface PdfSearchResult {
  page: number;
  snippet: string;
  count: number;
}

/** PDF 查看器（pdf.js 渲染到 canvas + 文本层，支持划词翻译） */
type PdfTranslation = { status: "loading" } | { status: "error"; message: string }
  | { status: "ready"; text: string; copy?: "done" | "failed" };

export function PdfPanel() {
  const { activeFile, currentCase, user, activePublication } = useStore();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const pageScrollRef = useRef<HTMLDivElement>(null);
  const numberGeneration = useRef(0);
  const numberInFlight = useRef(false);
  const viewportRef = useRef<{ doc: unknown; page: number; scale: number; rotation: number; viewport: any } | null>(null);
  const syncInFlight = useRef(false);
  const translationGeneration = useRef(0);
  const abortPublishedRead = useRef<(() => void) | null>(null);
  const [doc, setDoc] = useState<any>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(1);
  const [scale, setScale] = useState(1.2);
  const [rotation, setRotation] = useState(0);
  const [renderedLayer, setRenderedLayer] = useState<{
    doc: unknown; page: number; scale: number; rotation: number; items: PdfTextItem[]; divs: HTMLElement[];
  } | null>(null);
  const [err, setErr] = useState("");
  // 划词翻译：选区锚点 + 结果
  const [sel, setSel] = useState<{ x: number; y: number; text: string } | null>(null);
  const [trans, setTrans] = useState<PdfTranslation | null>(null);
  const [numbering, setNumbering] = useState(false);
  const [numberStatus, setNumberStatus] = useState("");
  const [outline, setOutline] = useState<PdfOutlineEntry[]>([]);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<PdfSearchResult[]>([]);
  const [searched, setSearched] = useState("");
  const [searchPage, setSearchPage] = useState(0);
  const [resultLimit, setResultLimit] = useState(50);
  const [matchFocus, setMatchFocus] = useState<{ page: number } | null>(null);
  const searchRead = useRef<AbortController | null>(null);
  const [notice, setNotice] = useState("");
  const [rangeStats, setRangeStats] = useState({ requests: 0, bytes: 0 });
  const [reload, setReload] = useState(0);
  const selection = activePublication?.caseId === currentCase?.id && activePublication?.path === activeFile ? activePublication : null;
  const publicationId = selection?.id ?? undefined;
  const main = activeFile ? latexMainForPdf(activeFile) : null;
  const [opened, setOpened] = useState<(PdfSelection & {caseId:number;path:string;version:string}) | null>(null);
  const selectedPdf = !selection?.invalid && opened?.caseId === currentCase?.id && opened?.path === activeFile
    && (!publicationId || opened.publication?.id === publicationId) ? opened : null;
  const publicationState = usePublicationStatus(user?.id, currentCase?.id, activeFile ? publicationChannel(activeFile) : null, selectedPdf?.publication ?? null);
  const [frozenSource, setFrozenSource] = useState<FrozenPdfSource | null>(null);
  const [sourceReload, setSourceReload] = useState(0);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceError, setSourceError] = useState('');
  const sourceHints = useRef(new Map<string, PublishedSourceHint | null>());
  const publishedSourceId = selectedPdf?.publication?.id;
  useEffect(() => {
    setFrozenSource(null); setSourceError(''); setSourceBusy(false);
    if (!user || !currentCase || !activeFile || !publishedSourceId || publicationState.blocked) return;
    let hint;
    try { hint = sourceHints.current.has(publishedSourceId) ? sourceHints.current.get(publishedSourceId)
      : readPublishedSourceHint(user.id, currentCase.id, activeFile, publishedSourceId); }
    catch (cause) { setSourceError(cause instanceof Error ? cause.message : String(cause)); return; }
    if (!hint) return;
    const controller = new AbortController();
    const generation = selectedPdf.publication!.generation;
    setSourceBusy(true);
    void (async () => {
      const response = await pdfRequest(`/api/v1/cases/${currentCase.id}/latex/source?path=${encodeURIComponent(hint.file)}`
        + `&publication_id=${encodeURIComponent(publishedSourceId)}`, controller.signal);
      if (!response.ok) throw new Error(`所选版本源码读取失败 (${response.status})；可重试或重新定位。`);
      const text = await response.text();
      if (hint.line > text.split('\n').length) throw new Error('保存的源码行超出所选版本，请重新定位。');
      if (!controller.signal.aborted) {
        setFrozenSource({ file: hint.file, line: hint.line, text, generation });
        setNotice(`已定位版本 ${generation} 的源码；工作区文件未改动`);
      }
    })().catch(cause => { if (!controller.signal.aborted) setSourceError(cause instanceof Error ? cause.message : String(cause)); })
      .finally(() => { if (!controller.signal.aborted) setSourceBusy(false); });
    return () => controller.abort();
  }, [user?.id, currentCase?.id, activeFile, publishedSourceId, publicationState.blocked, sourceReload]);
  const closeFrozenSource = () => {
    if (publishedSourceId) sourceHints.current.set(publishedSourceId, null);
    if (user && currentCase && activeFile && publishedSourceId) savePublishedSourceHint(user.id, currentCase.id, activeFile, publishedSourceId, null);
    setSourceReload(value => value + 1);
  };
  const searchGeneration = useRef(0);
  useEffect(() => {
    let cancelled = false;
    let loadingTask: any = null;
    let failure: Error | null = null;
    const headController = new AbortController();
    let loadedDoc: any = null;
    let rangeController: ReturnType<typeof createPdfRangeTransport> | null = null;
    const abortRead = () => {
      headController.abort(); rangeController?.abort();
      searchRead.current?.abort();
      if (loadedDoc) void Promise.resolve(loadedDoc.destroy()).catch(() => {});
      else if (loadingTask) void Promise.resolve(loadingTask.destroy()).catch(() => {});
    };
    abortPublishedRead.current = abortRead;
    numberGeneration.current += 1;
    numberInFlight.current = false;
    syncInFlight.current = false;
    viewportRef.current = null;
    searchGeneration.current += 1;
    setNumbering(false);
    setNumberStatus("");
    setOutline([]);
    setQuery("");
    setSearched("");
    setMatchFocus(null);
    setSearching(false);
    setSearchPage(0);
    setSearchResults([]);
    setNotice("");
    setRangeStats({ requests: 0, bytes: 0 });
    setOpened(null);
    setFrozenSource(null);
    (async () => {
      if (!activeFile || !currentCase) return;
      setErr("");
      setDoc(null);
      const caseId = currentCase.id;
      const path = activeFile;
      try {
        if (selection?.invalid) throw new Error('保存的版本选择无法核验，请明确选择所需发布。');
        const lib = await loadPdfjs();
        if (cancelled) return;
        const token = localStorage.getItem("vf_token") ?? "";
        const selected = await selectPdf(caseId, path, headController.signal, publicationId);
        const url = selected.url;
        const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
        const head = await fetch(url, { method: "HEAD", headers, signal: headController.signal, cache: "no-store" });
        if (!head.ok) throw new Error(`PDF HEAD 失败 (${head.status})`);
        const length = Number(head.headers.get("Content-Length"));
        if (!Number.isSafeInteger(length) || length <= 0) throw new Error("PDF Content-Length 无效");
        const version = head.headers.get("X-Vibeflow-File-Version") ?? "";
        if (!/^[0-9a-f]{64}$/.test(version)) throw new Error("PDF 缺少有效版本信息，请重新读取");
        if (cancelled) return;
        const range = createPdfRangeTransport(lib, {
          url, token, length, version,
          onRequest: (begin, end) => {
            if (!cancelled) setRangeStats((current) => ({ requests: current.requests + 1, bytes: current.bytes + end - begin }));
          },
          onError: (error) => {
            if (cancelled || failure) return;
            failure = error;
            searchGeneration.current += 1;
            setSearching(false);
            setDoc(null);
            setErr(error.message);
          },
        });
        rangeController = range;
        loadingTask = lib.getDocument({
          range: range.transport, length, disableAutoFetch: true, disableStream: false, rangeChunkSize: 65536,
          isEvalSupported: false,
          cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/",
        });
        range.setLoadingTask(loadingTask);
        loadedDoc = await loadingTask.promise;
        if (cancelled || failure) return;
        if (selected.publication && user) useStore.getState().rememberPublication(user.id, caseId, path, publicationId ?? null, selected.publication.id);
        setOpened({...selected,caseId,path,version});
        setDoc(loadedDoc);
        setPages(loadedDoc.numPages);
        const saved = readPdfView(user?.id ?? 0, caseId, path, selected.publication?.id ?? `file:${version}`);
        setPage(saved.page <= loadedDoc.numPages ? saved.page : 1);
        setScale(saved.scale);
        setRotation(saved.rotation ?? 0);
        const outlineItems = await loadedDoc.getOutline().catch(() => []);
        if (cancelled || failure) return;
        const flattened: PdfOutlineEntry[] = [];
        const flatten = (items: any[], depth: number) => {
          for (const item of items ?? []) {
            flattened.push({ title: item.title || "未命名", depth, dest: item.dest, url: item.url });
            if (item.items?.length) flatten(item.items, depth + 1);
          }
        };
        flatten(outlineItems ?? [], 0);
        setOutline(flattened);
      } catch (e: any) {
        if (!cancelled && !failure) setErr(String(e?.message ?? e));
      }
    })();
    return () => {
      cancelled = true;
      abortRead();
      searchGeneration.current += 1;
      if (abortPublishedRead.current === abortRead) abortPublishedRead.current = null;
    };
  }, [activeFile, currentCase?.id, user?.id, reload, publicationId, selection?.invalid]);

  useEffect(() => {
    if (!publicationState.blocked) return;
    abortPublishedRead.current?.(); searchGeneration.current += 1; numberGeneration.current += 1;
    setDoc(null); setFrozenSource(null); setErr(publicationState.error);
  }, [publicationState.blocked, publicationState.error]);

  useEffect(() => {
    let cancelled = false;
    let renderTask: any = null;
    let textTask: any = null;
    const textRead = new AbortController();
    viewportRef.current = null;
    setRenderedLayer(null);
    (async () => {
      if (!doc || !canvasRef.current || !textLayerRef.current) return;
      try {
        const lib = await loadPdfjs();
        if (cancelled) return;
        const p = await doc.getPage(page);
        if (cancelled) return;
        const viewport = p.getViewport({ scale, rotation: (p.rotate + rotation) % 360 });
        const canvas = canvasRef.current;
        if (!(viewport.width > 0 && viewport.height > 0)) throw new Error("PDF 页面尺寸无效");
        const rasterScale = Math.min(1, Math.sqrt(16_000_000 / (viewport.width * viewport.height)),
          8192 / viewport.width, 8192 / viewport.height);
        canvas.width = Math.max(1, Math.floor(viewport.width * rasterScale));
        canvas.height = Math.max(1, Math.floor(viewport.height * rasterScale));
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        const tl = textLayerRef.current;
        tl.innerHTML = "";
        tl.style.width = `${viewport.width}px`;
        tl.style.height = `${viewport.height}px`;
        tl.style.setProperty("--scale-factor", String(viewport.scale));
        renderTask = p.render({ canvasContext: canvas.getContext("2d")!, viewport,
          transform: rasterScale === 1 ? undefined : [canvas.width / viewport.width, 0, 0, canvas.height / viewport.height, 0, 0] });
        await renderTask.promise;
        if (cancelled) return;
        const textContent = await readPdfText(p, textRead.signal);
        if (cancelled) return;
        const divs: HTMLElement[] = [];
        textTask = lib.renderTextLayer({ textContent, container: tl, viewport, textDivs: divs });
        await textTask.promise;
        if (cancelled) return;
        viewportRef.current = { doc, page, scale, rotation, viewport };
        setRenderedLayer({ doc, page, scale, rotation, items: textContent.items, divs });
      } catch (error: any) {
        if (!cancelled && error?.name !== "RenderingCancelledException") setErr(current => current || String(error?.message ?? error));
      }
    })();
    return () => {
      cancelled = true;
      renderTask?.cancel();
      textTask?.cancel();
      textRead.abort();
    };
  }, [doc, page, scale, rotation]);

  const currentLayer = renderedLayer?.doc === doc && renderedLayer?.page === page
    && renderedLayer?.scale === scale && renderedLayer?.rotation === rotation ? renderedLayer : null;
  const highlights = useMemo(() => currentLayer && searched && wrapRef.current
    ? pdfHighlightRects(currentLayer.items, currentLayer.divs, wrapRef.current, searched) : [], [currentLayer, searched]);

  useEffect(() => { pageScrollRef.current?.scrollTo(0, 0); }, [doc, page]);
  useEffect(() => {
    if (matchFocus?.page === page && highlights.length) {
      wrapRef.current?.querySelector(".pdf-search-hit")?.scrollIntoView({ block: "center", inline: "nearest" });
      setMatchFocus(null);
    }
  }, [matchFocus, highlights, page]);

  useEffect(() => {
    setSel(null);
    setTrans(null);
    return () => { translationGeneration.current += 1; };
  }, [doc, page, scale, rotation, activeFile, currentCase?.id, user?.id, publicationId, reload]);

  useEffect(() => {
    if (!activeFile || !currentCase || !user || !doc || !selectedPdf) return;
    savePdfView(user.id, currentCase.id, activeFile, selectedPdf.publication?.id ?? `file:${selectedPdf.version}`, page, scale, rotation);
  }, [doc, page, scale, rotation, activeFile, currentCase?.id, user?.id, selectedPdf]);

  const openOutline = async (entry: PdfOutlineEntry) => {
    if (!doc) return;
    const generation = numberGeneration.current;
    if (entry.url) {
      if (/^https?:\/\//i.test(entry.url)) window.open(entry.url, "_blank", "noopener,noreferrer");
      else setNotice("已阻止不安全的目录链接");
      return;
    }
    try {
      const destination = typeof entry.dest === "string" ? await doc.getDestination(entry.dest) : entry.dest;
      const reference = Array.isArray(destination) ? destination[0] : null;
      if (reference == null) throw new Error("目录没有页面对象");
      const index = typeof reference === "number" ? reference : await doc.getPageIndex(reference);
      if (!Number.isInteger(index) || index < 0 || index >= doc.numPages) throw new Error("目录页超出文档范围");
      if (generation !== numberGeneration.current) return;
      setPage(index + 1);
      setNotice(`已定位第 ${index + 1} 页`);
    } catch (error: any) {
      if (generation === numberGeneration.current) setNotice(`目录定位失败：${error?.message ?? String(error)}`);
    }
  };

  const runSearch = async () => {
    const needle = pdfSearchQuery(query);
    if (!doc || !needle) return;
    searchRead.current?.abort();
    const controller = new AbortController();
    searchRead.current = controller;
    const generation = ++searchGeneration.current;
    setSearching(true);
    setSearchPage(0);
    setSearched(needle);
    setSearchResults([]);
    setResultLimit(50);
    setNotice("");
    try {
      const results: PdfSearchResult[] = [];
      for (let pageNumber = 1; pageNumber <= doc.numPages; pageNumber++) {
        if (controller.signal.aborted || generation !== searchGeneration.current) return;
        const pdfPage = await doc.getPage(pageNumber);
        const content = await readPdfText(pdfPage, controller.signal);
        const result = pdfPageSearch(content.items, needle);
        if (result) results.push({ page: pageNumber, ...result });
        if (pageNumber % 8 === 0) setSearchPage(pageNumber);
      }
      if (generation !== searchGeneration.current) return;
      setSearchResults(results);
      setNotice(results.length ? `找到 ${results.length} 个匹配页，共 ${results.reduce((sum, result) => sum + result.count, 0)} 处` : "未找到匹配文本");
    } catch (error: any) {
      if (!controller.signal.aborted && generation === searchGeneration.current) setNotice(`搜索失败：${error?.message ?? String(error)}`);
    } finally {
      if (searchRead.current === controller) searchRead.current = null;
      if (generation === searchGeneration.current) setSearching(false);
    }
  };

  const clearSearch = (message = "") => {
    searchGeneration.current += 1;
    searchRead.current?.abort();
    setSearching(false);
    setSearched("");
    setMatchFocus(null);
    setSearchResults([]);
    setNotice(message);
  };

  const addPageNumbers = async () => {
    if (!activeFile || !currentCase || numberInFlight.current) return;
    const generation = ++numberGeneration.current;
    numberInFlight.current = true;
    setNumbering(true);
    setNumberStatus("提交页码作业…");
    try {
      const job = await api.post<{ job_id: number; output: string }>(
        `/cases/${currentCase.id}/pdf:number`, { input: activeFile });
      let terminal: any = null;
      for (let i = 0; i < 240; i++) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        if (generation !== numberGeneration.current) return;
        const state = await api.get<{ status: string; log_backlog?: string }>(`/jobs/${job.job_id}`);
        if (["succeeded", "failed", "cancelled", "interrupted"].includes(state.status)) {
          terminal = state;
          break;
        }
      }
      if (!terminal) throw new Error("页码作业等待超时");
      if (terminal.status !== "succeeded") {
        throw new Error(`页码作业${terminal.status}：${(terminal.log_backlog ?? "").slice(-300)}`);
      }
      if (generation !== numberGeneration.current) return;
      setNumberStatus(`已生成 ${job.output}`);
      await useStore.getState().loadTree();
      if (generation !== numberGeneration.current) return;
      await useStore.getState().openFile(job.output);
    } catch (error: any) {
      if (generation === numberGeneration.current) {
        setNumberStatus(`页码生成失败：${error?.message ?? String(error)}`);
      }
    } finally {
      if (generation === numberGeneration.current) {
        numberInFlight.current = false;
        setNumbering(false);
      }
    }
  };

  const jumpToSource = async (event: React.MouseEvent) => {
    const rendered = viewportRef.current;
    if (!activeFile || !currentCase || !selectedPdf || !doc || !rendered || syncInFlight.current
      || rendered.doc !== doc || rendered.page !== page || rendered.scale !== scale || rendered.rotation !== rotation) return;
    const bounds = wrapRef.current?.getBoundingClientRect();
    if (!bounds) return;
    // Keep the absolute PDF point so the server can use the selected version's MediaBox.
    const [x, y] = rendered.viewport.convertToPdfPoint(event.clientX - bounds.left, event.clientY - bounds.top);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    syncInFlight.current = true;
    const generation = numberGeneration.current;
    setNotice("SyncTeX 定位中…");
    try {
      const result = await api.post<{ file: string; line: number; publication_id?: string }>(`/cases/${currentCase.id}/synctex:query`,
        { pdf: activeFile, page, x, y, coordinate_space: 'pdf', ...(selectedPdf.publication ? {publication_id: selectedPdf.publication.id}
          : {expected_file_version:selectedPdf.version}) });
      if (generation !== numberGeneration.current) return;
      if (selectedPdf?.publication) {
        if (result.publication_id !== selectedPdf.publication.id) throw new Error("源码定位返回了其他发布版本");
        if (!user) throw new Error('请登录后读取历史源码');
        const hint = {file:result.file,line:result.line};
        savePublishedSourceHint(user.id, currentCase.id, activeFile, selectedPdf.publication.id, hint);
        sourceHints.current.set(selectedPdf.publication.id, hint);
        setSourceReload(value => value + 1);
        return;
      }
      if (!await useStore.getState().openFile(result.file)) return;
      activatePanel("editor");
      revealLine(result.line, result.file);
      setNotice(`已定位 ${result.file}:${result.line}`);
    } catch (error: any) {
      if (generation === numberGeneration.current) setNotice(`PDF 回跳失败：${error?.message ?? String(error)}`);
    } finally {
      if (generation === numberGeneration.current) syncInFlight.current = false;
    }
  };

  const clearTranslation = () => {
    translationGeneration.current += 1;
    setSel(null);
    setTrans(null);
  };

  const updateTranslationSelection = () => {
    const selection = window.getSelection();
    const text = selection?.toString().trim();
    const layer = textLayerRef.current;
    if (!text || !selection?.rangeCount || !wrapRef.current || !layer?.contains(selection.anchorNode)
        || !layer.contains(selection.focusNode)) {
      clearTranslation();
      return;
    }
    const range = selection.getRangeAt(0).getBoundingClientRect();
    const host = wrapRef.current.getBoundingClientRect();
    translationGeneration.current += 1;
    setSel({ x: range.left - host.left + range.width / 2, y: range.bottom - host.top + 6, text });
    setTrans(null);
  };

  const doTranslate = async () => {
    if (!sel || trans?.status === "loading") return;
    const generation = ++translationGeneration.current;
    setTrans({ status: "loading" });
    const target = /\p{Script=Han}/u.test(sel.text) ? "en" : "zh";
    try {
      const r = await api.post<{ translation: string }>("/llm/translate",
        { text: sel.text.slice(0, 4000), target });
      if (!r.translation.trim()) throw new Error("模型未返回译文");
      if (generation === translationGeneration.current) setTrans({ status: "ready", text: r.translation });
    } catch (error) {
      if (generation === translationGeneration.current) setTrans({ status: "error",
        message: error instanceof Error ? error.message : String(error) });
    }
  };

  const copyTranslation = async () => {
    if (trans?.status !== "ready") return;
    const generation = translationGeneration.current;
    let copy: "done" | "failed" = "done";
    try { await navigator.clipboard.writeText(trans.text); }
    catch { copy = "failed"; }
    if (generation === translationGeneration.current) setTrans({ ...trans, copy });
  };

  const reloadSelected = () => {
    if (user && currentCase && activeFile && main && !selection?.invalid) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, selectedPdf?.publication?.id ?? publicationId ?? null);
    setReload(value => value + 1);
  };
  const openLatest = () => {
    if (user && currentCase && activeFile && main) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, null);
    setReload(value => value + 1);
  };
  const history = main && currentCase && activeFile ? <ReportHistory key={`${currentCase.id}:${activeFile}`}
    main={main} caseId={currentCase.id} selectedId={selectedPdf?.publication?.id}
    onSelect={id => { if (user) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, id); setReload(value=>value+1); }} /> : null;
  const downloadPdf = async () => {
    if (!selectedPdf || !doc || err || publicationState.blocked) return;
    const generation=numberGeneration.current;
    try {
      const response=await pdfRequest(selectedPdf.url,undefined,selectedPdf.version);
      if (!response.ok || response.headers.get("X-Vibeflow-File-Version") !== selectedPdf.version)
        throw new Error("PDF 版本读取失败，请重新读取");
      const blob=await response.blob();
      if(generation!==numberGeneration.current)return;
      const url=URL.createObjectURL(blob), anchor=document.createElement("a");
      anchor.href=url; anchor.download=selectedPdf.path.split("/").pop() || "report.pdf";
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
      setNotice("已开始下载当前显示版本的 PDF 原件");
    } catch(error) { if(generation===numberGeneration.current) setNotice(error instanceof Error ? error.message : String(error)); }
  };
  if (!activeFile) return <div style={s.hint}>未选择文件</div>;
  if (err || publicationState.blocked) return <div style={s.hint}>
    {history}
    <div role="alert" data-testid="pdf-error">PDF 加载失败：{publicationState.blocked ? publicationState.error : err}</div>
    <div className="publication-reading-actions"><button data-testid="pdf-reload" style={s.miniBtn} disabled={selection?.invalid} onClick={reloadSelected}>重读所选版本</button>
      {main && <button data-testid="pdf-latest" style={s.miniBtn} onClick={openLatest}>打开最新发布</button>}</div>
  </div>;

  return (
    <div data-testid="pdf-viewer" data-file={activeFile}
         data-range-requests={rangeStats.requests} data-range-bytes={rangeStats.bytes} data-scale={scale} data-rotation={rotation}
         data-rendered-page={currentLayer?.page}
         style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div className="pdf-version-toolbar publication-reading-actions pdf-reading-toolbar" style={s.bar}>
        <button data-testid="pdf-reload" style={s.miniBtn} onClick={reloadSelected}>{main ? '重读所选版本' : '重新读取'}</button>
        {main && <button data-testid="pdf-latest" style={s.miniBtn} onClick={openLatest}>打开最新发布</button>}
        <button data-testid="pdf-download" style={s.miniBtn} disabled={!selectedPdf || !doc} onClick={()=>void downloadPdf()}>下载原件</button>
        <button data-testid="pdf-prev" style={s.miniBtn} disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
        <span data-testid="pdf-page-indicator" style={{ fontSize: 12, color: "#9ab" }}>{page} / {pages}</span>
        <button data-testid="pdf-next" style={s.miniBtn} disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button>
        <form className="pdf-go-to-page" onSubmit={event => {
          event.preventDefault();
          const target = Number(new FormData(event.currentTarget).get("page"));
          if (doc && Number.isInteger(target) && target >= 1 && target <= pages) setPage(target);
        }}>
          <input key={page} name="page" aria-label="PDF 页码" type="number" min={1} max={pages} defaultValue={page}
                 disabled={!doc} style={{ ...s.input, minWidth: 0 }} required />
          <button style={s.miniBtn} disabled={!doc}>跳页</button>
        </form>
        <button data-testid="pdf-zoom-out" aria-label="缩小 PDF" style={s.miniBtn} onClick={() => setScale((x) => Math.max(0.4, x - 0.2))}>−</button>
        <button data-testid="pdf-zoom-in" aria-label="放大 PDF" style={s.miniBtn} onClick={() => setScale((x) => Math.min(3, x + 0.2))}>+</button>
        <button data-testid="pdf-rotate" style={s.miniBtn} disabled={!doc} onClick={() => setRotation(value => (value + 90) % 360)}>
          {rotation ? `旋转 ${rotation}°` : "旋转"}
        </button>
        <button data-testid="pdf-number" style={s.miniBtn} disabled={numbering} onClick={addPageNumbers}>
          {numbering ? "页码生成中…" : selectedPdf?.publication ? "为工作区文件添加页码" : "添加页码"}
        </button>
        <span data-testid="pdf-range-info" title="PDF.js 按需 Range 请求" style={{ fontSize: 10, color: "#667" }}>
          Range {rangeStats.requests} · {(rangeStats.bytes / 1024).toFixed(0)} KB
        </span>
        {numberStatus && <span data-testid="pdf-number-status" style={{ fontSize: 11, color: "#9ab" }}>{numberStatus}</span>}
        <span style={{ fontSize: 11, color: "#667", marginLeft: "auto" }}>划选翻译 · 双击回跳源码</span>
      </div>
      {history}
      {selectedPdf?.publication && <div data-testid="pdf-publication" role="status" style={{color:"var(--vf-text)",padding:"6px 10px",fontSize:14,lineHeight:1.6}}>
        {publicationState.error || `PDF ${publicationLabel(publicationState.publication ?? selectedPdf.publication)}`}
      </div>}
      {sourceBusy && <div className="pdf-source-status" role="status">正在重新核验所选发布的历史源码…</div>}
      {sourceError && <div className="pdf-source-status" role="alert" data-testid="pdf-source-error">{sourceError}
        <button type="button" onClick={() => setSourceReload(value => value + 1)}>重试所选源码</button>
        <button type="button" onClick={closeFrozenSource}>关闭源码</button>
      </div>}
      {frozenSource && <PdfSource source={frozenSource} close={closeFrozenSource} reread={() => setSourceReload(value => value + 1)} />}
      {notice && <div data-testid="pdf-notice" style={{ color: "#9ab", padding: "3px 8px", fontSize: 11 }}>{notice}</div>}
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <aside className="pdf-reading-sidebar">
          <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
            <input data-testid="pdf-search" aria-label="全文搜索 PDF" value={query} onChange={(event) => {
              setQuery(event.target.value); clearSearch();
            }}
                   onKeyDown={(event) => { if (event.key === "Enter") void runSearch(); }}
                   placeholder="全文搜索…" style={{ ...s.input, minWidth: 0, width: 125 }} />
            <button data-testid="pdf-search-run" style={s.miniBtn} disabled={!doc || searching || !query.trim()} onClick={runSearch}>
              搜索
            </button>
          </div>
          {searching && <div className="pdf-search-progress" role="status">
            <span>已搜索 {searchPage} / {pages} 页</span>
            <button data-testid="pdf-search-cancel" style={s.miniBtn} onClick={() => clearSearch("已取消搜索")}>取消</button>
          </div>}
          {searchResults.slice(0, resultLimit).map((result) => (
            <button key={result.page} data-testid="pdf-search-result" data-page={result.page}
                    aria-current={result.page === page ? "page" : undefined}
                    onClick={() => { setPage(result.page); setMatchFocus({ page: result.page }); }} style={s.pdfNavButton}>
              第 {result.page} 页 · {result.count} 处<br />{result.snippet}
            </button>
          ))}
          {searchResults.length > resultLimit && <button style={s.miniBtn} onClick={() => setResultLimit(value => value + 50)}>更多匹配页</button>}
          <div style={{ color: "#667", fontSize: 11, padding: "7px 4px" }}>目录（{outline.length}）</div>
          {outline.map((entry, index) => (
            <button key={`${entry.title}-${index}`} data-testid="pdf-outline-item" title={entry.title}
                    onClick={() => { void openOutline(entry); }}
                    style={{ ...s.pdfNavButton, paddingLeft: 6 + entry.depth * 12 }}>
              {entry.title}
            </button>
          ))}
          {!outline.length && <div style={{ color: "#667", fontSize: 11, padding: 4 }}>无 PDF 目录</div>}
        </aside>
        <div ref={pageScrollRef} data-testid="pdf-page-scroll" style={{ flex: 1, minWidth: 0, overflow: "auto", display: "flex", background: "#0b0d12", padding: 12 }}>
        <div ref={wrapRef} style={{ position: "relative", alignSelf: "flex-start", flexShrink: 0, marginInline: "auto" }}
             aria-busy={!currentLayer} onMouseUp={updateTranslationSelection} onDoubleClick={jumpToSource}>
          <canvas ref={canvasRef} style={{ boxShadow: "0 4px 24px #0008", display: "block" }} />
          {highlights.length > 0 && <div className="pdf-search-overlay" aria-hidden="true">
            {highlights.map((hit, index) => <div key={index} className="pdf-search-hit" data-testid="pdf-search-highlight"
              style={{ left: hit.x, top: hit.y, width: hit.width, height: hit.height }} />)}
          </div>}
          <div ref={textLayerRef} className="textLayer" />
          {sel && (
            <div onMouseUp={event => event.stopPropagation()} onDoubleClick={event => event.stopPropagation()}
                 onKeyDown={event => { if (event.key === "Escape") clearTranslation(); }}
                 style={{ position: "absolute", top: sel.y, transform: "translateX(-50%)", zIndex: 10,
                           left: trans ? `clamp(min(170px, 50%), ${sel.x}px, max(calc(100% - 170px), 50%))` : sel.x,
                           width: trans ? "min(340px, 100%)" : undefined }}>
              {!trans ? (
                <button data-testid="pdf-translate" style={s.transBtn} onClick={doTranslate}>翻译选中文本</button>
              ) : (
                <div data-testid="pdf-translation" style={s.transCard}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontSize: 12, color: "#a7b4c7" }}>翻译</span>
                    <span>
                      {trans.status === "ready" && <button data-testid="pdf-copy-translation" style={s.transClose}
                          onClick={copyTranslation}>复制译文</button>}
                      {trans.status === "error" && <button style={s.transClose} onClick={doTranslate}>重试翻译</button>}
                      <button style={s.transClose} onClick={clearTranslation}>关闭</button>
                    </span>
                  </div>
                  <div data-testid="pdf-translation-content" role="status" style={{ fontSize: 13, color: "#d7dce5",
                       whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 200, overflow: "auto" }}>
                    {trans.status === "ready" ? trans.text : trans.status === "error" ? `翻译失败：${trans.message}` : "翻译中…"}
                  </div>
                  {trans.status === "ready" && trans.copy && <div role="status" style={{ color: "#a7b4c7", fontSize: 12 }}>
                    {trans.copy === "done" ? "译文已复制" : "复制失败，请选择译文后复制"}
                  </div>}
                </div>
              )}
            </div>
          )}
        </div>
        </div>
      </div>
    </div>
  );
}

/** texo 公式识别：iframe 里的原版识别页是纯计算 worker（无 UI，console.info 信令）。
 *  本面板补上完整 UI：选图（算例图片/本地文件）→ 调 vfTexoRecognize → LaTeX → 复制/插入编辑器。
 *  信令协议（逆向契约）：console.info("VIBEFLOW_TEXO:{type}:{req}:{base64url(payload)}") */
export function TexoPanel() {
  const { user, currentCase } = useStore();
  if (!user) return null;
  if (import.meta.env.VITE_TEXO_AVAILABLE !== "true") {
    return <p role="status" style={{ padding: 10, color: "var(--vf-text)" }}>
      公式识别资源尚未安装。恢复本地资产后重启开发服务或重新构建。
    </p>;
  }
  return <TexoWorkspace key={`${user.id}:${currentCase?.id ?? "local"}`} accountId={user.id} />;
}

type TexoWorker = Window & { vfTexoRecognize?: (image: string, request: number) => Promise<void> };

function TexoWorkspace({ accountId }: { accountId: number }) {
  const { currentCase, fileTree } = useStore();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState("等待页面加载…");
  const [busy, setBusy] = useState(false);
  const [latex, setLatex] = useState("");
  const [wrapMode, setWrapMode] = useState("inline");   // 插入格式（对齐原版格式选择器）
  const [imgSrc, setImgSrc] = useState("");          // 算例内图片路径
  const [preview, setPreview] = useState("");        // dataURL 预览
  const reqRef = useRef(0);
  const selected = useRef(0);
  const recognition = useRef<{ request: number; selection: number; hash: string | null } | undefined>(undefined);
  const receiptStore = useMemo(() => new TexoReceiptStore(accountId), [accountId]);
  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const captureRequest = useRef(0);
  const capturePending = useRef(false);
  const dragPointer = useRef<number | null>(null);
  const [captureStatus, setCaptureStatus] = useState<"idle" | "requesting" | "sharing">("idle");
  const [videoReady, setVideoReady] = useState(false);
  const [cropStart, setCropStart] = useState<CropPoint | null>(null);
  const [cropEnd, setCropEnd] = useState<CropPoint | null>(null);
  useEffect(() => {
    void receiptStore.prune();
    return () => { selected.current += 1; recognition.current = undefined; };
  }, [receiptStore]);

  const decode = (b64url: string) => {
    const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  };

  // iframe 加载后挂钩 console.info（同源，直接包一层）
  const onIframeLoad = () => {
    const w = iframeRef.current?.contentWindow as TexoWorker | null;
    if (!w) return;
    const logger = (w as unknown as { console: Console }).console;
    const orig = logger.info.bind(logger);
    logger.info = (...args: unknown[]) => {
      orig(...args);
      const m = String(args[0] ?? "").match(/^VIBEFLOW_TEXO:([\w-]+):(\d+):(.*)$/);
      if (!m) return;
      const [, type, request, payload] = m;
      if (type === "ready") {
        setReady(true);
        if (!selected.current) setStatus("识别引擎已就绪，请选择公式图片");
        return;
      }
      if (type === "bootstrap-error") {
        setReady(false);
        setStatus("识别引擎加载失败，请刷新页面后重试");
        return;
      }
      const run = recognition.current;
      if (!run || run.request !== Number(request)) return;
      const text = decode(payload);
      if (type === "result" && run.hash) void receiptStore.save(run.hash, text);
      if (run.selection !== selected.current) return;
      if (type === "progress") setStatus(text);
      else if (type === "result") {
        setLatex(text);
        setStatus(text ? "识别完成" : "未识别到公式，请换一张清晰的图片");
      } else if (type === "error") setStatus("识别失败：" + text.slice(0, 200));
    };
    if (w.vfTexoRecognize) {
      setReady(true);
      if (!selected.current) setStatus("识别引擎已就绪，请选择公式图片");
    }
  };

  const blobToDataUrl = (blob: Blob) => new Promise<string>((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.onerror = fr.onabort = () => reject(new Error("无法读取图片，请重新选择"));
    fr.readAsDataURL(blob);
  });

  const selectImage = async (path: string, read?: () => Promise<string>) => {
    const selection = ++selected.current;
    setImgSrc(path); setPreview(""); setLatex("");
    setStatus(read ? "正在读取图片…" : "请选择公式图片");
    if (!read) return;
    try {
      const dataUrl = await read();
      if (selection !== selected.current) return;
      setPreview(dataUrl);
      const hash = await receiptStore.hash(dataUrl);
      const cached = hash ? await receiptStore.load(hash) : null;
      if (selection !== selected.current || recognition.current?.selection === selection) return;
      setLatex(cached?.latex ?? "");
      setStatus(cached ? `缓存命中（${cached.hash.slice(0, 8)}）` : "图片已就绪，可开始识别");
    } catch (error) {
      if (selection === selected.current) setStatus(error instanceof Error ? error.message : "无法读取图片，请重新选择");
    }
  };

  const loadCaseImage = (path: string) => {
    if (!currentCase || !path) return selectImage("");
    return selectImage(path, async () => {
      const token = localStorage.getItem("vf_token");
      const response = await fetch(`/api/v1/cases/${currentCase.id}/raw?path=${encodeURIComponent(path)}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!response.ok) throw new Error("无法加载算例图片，请重新选择");
      return blobToDataUrl(await response.blob());
    });
  };

  const recognize = async () => {
    const w = iframeRef.current?.contentWindow as TexoWorker | null;
    if (!w?.vfTexoRecognize || !preview || recognition.current) return;
    const run = { request: ++reqRef.current, selection: selected.current, hash: null as string | null };
    recognition.current = run;
    setBusy(true);
    try {
      run.hash = await receiptStore.hash(preview);
      const cached = run.hash ? await receiptStore.load(run.hash) : null;
      if (run.selection !== selected.current) return;
      if (cached) {
        setLatex(cached.latex);
        setStatus(`缓存命中（${cached.hash.slice(0, 8)}）`);
        return;
      }
      setLatex(""); setStatus("启动识别…");
      await w.vfTexoRecognize(preview, run.request);
    } catch (error) {
      if (run.selection === selected.current) setStatus("调用失败：" + String(error));
    } finally {
      if (recognition.current === run) {
        recognition.current = undefined;
        setBusy(false);
      }
    }
  };

  const insertToEditor = () => {
    if (!latex) return;
    // 包裹格式（语义对齐原版：行内 $…$ / 展示 $$…$$ / equation 环境）
    const wrapped = latex.includes("$") || latex.includes("\\begin") ? latex
      : wrapMode === "inline" ? `$${latex}$`
      : wrapMode === "display" ? `$$\n${latex}\n$$`
      : `\\begin{equation}\n${latex}\n\\end{equation}`;
    if (!insertAtCursor(wrapped)) {
      setStatus("编辑器没有打开的文件——先在中间打开一个文本文件");
    } else {
      setStatus("已插入编辑器光标处");
    }
  };

  const stopCapture = (message = "屏幕共享已停止") => {
    captureRequest.current += 1;
    capturePending.current = false;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    dragPointer.current = null;
    setVideoReady(false);
    setCaptureStatus("idle");
    setCropStart(null);
    setCropEnd(null);
    setStatus(message);
  };

  useEffect(() => () => {
    captureRequest.current += 1;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    capturePending.current = false;
  }, []);

  useEffect(() => {
    if (captureStatus !== "sharing" || !videoRef.current || !streamRef.current) return;
    const video = videoRef.current;
    video.srcObject = streamRef.current;
    return () => { video.srcObject = null; };
  }, [captureStatus]);

  const startCapture = async () => {
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setStatus("当前浏览器不支持屏幕捕获，请使用本地图片"); return;
    }
    if (capturePending.current || streamRef.current) return;
    const request = ++captureRequest.current;
    capturePending.current = true;
    setCaptureStatus("requesting");
    setStatus("请在浏览器里选择共享来源");
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({video: true, audio: false});
      if (captureRequest.current !== request) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        if (streamRef.current === stream) stopCapture();
      }, { once: true });
      setCaptureStatus("sharing");
      setStatus("正在接收共享画面…");
    } catch (error) {
      if (captureRequest.current !== request) return;
      setCaptureStatus("idle");
      setStatus(error instanceof DOMException && error.name === "NotAllowedError"
        ? "屏幕共享已取消或未授权"
        : "屏幕捕获未启动：" + (error instanceof Error ? error.message : String(error)));
    } finally {
      if (captureRequest.current === request) capturePending.current = false;
    }
  };

  const cropPoint = (event: React.PointerEvent<HTMLDivElement>): CropPoint => {
    const rect = event.currentTarget.getBoundingClientRect();
    const unit = (value: number) => Math.min(1, Math.max(0, value));
    return {x: unit((event.clientX - rect.left) / rect.width),
            y: unit((event.clientY - rect.top) / rect.height)};
  };
  const applyCrop = async () => {
    const video = videoRef.current;
    if (!videoReady || !video?.videoWidth || !video.videoHeight || !cropStart || !cropEnd) return;
    const crop = normalizeCrop({x: cropStart.x * video.videoWidth, y: cropStart.y * video.videoHeight},
                               {x: cropEnd.x * video.videoWidth, y: cropEnd.y * video.videoHeight},
                               video.videoWidth, video.videoHeight);
    const canvas = document.createElement("canvas");
    canvas.width = crop.width; canvas.height = crop.height;
    canvas.getContext("2d")!.drawImage(video, crop.x, crop.y, crop.width, crop.height,
                                      0, 0, crop.width, crop.height);
    const dataUrl = canvas.toDataURL("image/png");
    stopCapture();
    await selectImage("", async () => dataUrl);
  };

  const caseImages = fileTree.filter((t) =>
    /\.(png|jpg|jpeg|webp|bmp|gif)$/i.test(t.path));

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 13,
                  color: "#d7dce5", padding: 10, gap: 8, overflow: "auto" }}>
      {/* 隐藏的计算 iframe（原版识别页无 UI） */}
      <iframe ref={iframeRef} src="/texo/texo-ocr.html" title="texo-worker"
              onLoad={onIframeLoad}
              style={{ width: 4, height: 4, border: "none", position: "absolute", opacity: 0.01,
                       pointerEvents: "none" }} />
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select style={s.select} value={imgSrc} onChange={(e) => loadCaseImage(e.target.value)}>
          <option value="">从算例选图片…</option>
          {caseImages.map((t) => <option key={t.path} value={t.path}>{t.path}</option>)}
        </select>
        <label style={s.miniBtn}>
          本地图片
          <input type="file" accept="image/*" style={{ display: "none" }} onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void selectImage("", () => blobToDataUrl(file));
            e.target.value = "";
          }} />
        </label>
        <button data-testid="screen-capture" style={s.miniBtn} disabled={captureStatus === "requesting"}
                onClick={captureStatus === "sharing" ? () => stopCapture() : startCapture}>
          {captureStatus === "sharing" ? "停止共享" : "屏幕框选"}
        </button>
        <button style={s.btn} onClick={recognize} disabled={!ready || !preview || busy}>
          {busy ? "识别中…" : "识别公式"}
        </button>
      </div>
      {captureStatus === "sharing" && (
        <div>
          <div data-testid="screen-crop-area" style={{ position: "relative", width: "fit-content", maxWidth: "100%",
                 cursor: "crosshair", touchAction: "none" }}
               onPointerDown={(event) => {
                 if (!videoReady || event.button !== 0 || dragPointer.current !== null) return;
                 const point = cropPoint(event);
                 dragPointer.current = event.pointerId;
                 setCropStart(point); setCropEnd(point);
                 event.currentTarget.setPointerCapture(event.pointerId);
               }}
               onPointerMove={(event) => {
                 if (dragPointer.current === event.pointerId) setCropEnd(cropPoint(event));
               }}
               onPointerUp={(event) => {
                 if (dragPointer.current !== event.pointerId) return;
                 dragPointer.current = null;
                 setCropEnd(cropPoint(event));
               }}
               onPointerCancel={(event) => {
                 if (dragPointer.current !== event.pointerId) return;
                 dragPointer.current = null;
                 setCropStart(null); setCropEnd(null);
               }}>
            <video ref={videoRef} muted autoPlay playsInline
                   onLoadedData={() => { setVideoReady(true); setStatus("请拖动框选公式区域"); }}
                   onError={() => stopCapture("无法读取共享画面")}
                   style={{ display: "block", maxWidth: "100%", maxHeight: 320, background: "#000" }} />
            {cropStart && cropEnd && <div data-testid="screen-crop-selection" style={{ position: "absolute",
              left: `${Math.min(cropStart.x, cropEnd.x) * 100}%`, top: `${Math.min(cropStart.y, cropEnd.y) * 100}%`,
              width: `${Math.abs(cropEnd.x - cropStart.x) * 100}%`, height: `${Math.abs(cropEnd.y - cropStart.y) * 100}%`,
              boxSizing: "border-box", border: "2px solid #3b82f6", background: "#3b82f622", pointerEvents: "none" }} />}
          </div>
          <button data-testid="screen-crop-apply" style={s.btn} onClick={applyCrop}
                  disabled={!videoReady || !cropStart || !cropEnd}>截取选中区域</button>
        </div>
      )}
      {preview && (
        <div style={{ textAlign: "center", background: "#fff", borderRadius: 6, padding: 6 }}>
          <img src={preview} alt="待识别" style={{ maxWidth: "100%", maxHeight: 140 }} />
        </div>
      )}
      <div role="status" data-testid="texo-ocr-status" style={{ fontSize: 12, color: "#8b93a7" }}>{status}</div>
      {latex && (
        <>
          <textarea aria-label="识别结果" style={s.result} value={latex} onChange={(e) => setLatex(e.target.value)} />
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <button style={s.miniBtn}
                    onClick={() => navigator.clipboard.writeText(latex)}>复制 LaTeX</button>
            <select style={s.selectNarrow} value={wrapMode}
                    onChange={(e) => setWrapMode(e.target.value)} title="插入包裹格式">
              <option value="inline">行内 $…$</option>
              <option value="display">展示 $$…$$</option>
              <option value="equation">equation 环境</option>
            </select>
            <button style={s.btn} onClick={insertToEditor}>插入编辑器光标处</button>
          </div>
        </>
      )}
      {!ready && <div style={s.hint}>识别引擎加载中（texo/FormulaNet，纯浏览器 WASM，不出网）…</div>}
    </div>
  );
}

export { TablePanel };

/** 查看器分发面板：按扩展名选择查看器 */
export default function ViewerPanel() {
  const { activeFile, currentCase, user } = useStore();
  const key = `${user?.id}:${currentCase?.id}:${activeFile}`;
  const ext = (activeFile ?? "").split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(ext)) return <ImagePanel />;
  if (ext === "pdf") return <PdfPanel key={key} />;
  if (["csv", "tsv"].includes(ext)) return <TablePanel />;
  if (["html", "htm", "docx"].includes(ext)) return <DocumentPanel key={key} />;
  return <div style={s.hint}>该文件类型暂无专用查看器（{ext || "未知"}），请在编辑器中查看</div>;
}

const s: Record<string, React.CSSProperties> = {
  hint: { padding: 16, color: "#8b93a7" },
  bar: { display: "flex", gap: 6, padding: 6, borderBottom: "1px solid #2a2f3a", alignItems: "center" },
  miniBtn: { padding: "3px 10px", background: "#1a1e27", color: "#9ab", border: "1px solid #2a2f3a", borderRadius: 5, cursor: "pointer", fontSize: 12 },
  transBtn: { padding: "2px 10px", background: "#2563eb", color: "#fff", border: "none",
              borderRadius: 5, cursor: "pointer", fontSize: 12, boxShadow: "0 2px 8px #0006" },
  transCard: { background: "#14171d", border: "1px solid #2a2f3a", borderRadius: 8,
               padding: "8px 10px", boxShadow: "0 6px 20px #000a", minWidth: 220 },
  transClose: { background: "transparent", color: "#a7b4c7", border: "none", cursor: "pointer", fontSize: 12 },
  input: { padding: "3px 6px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 5, fontSize: 11 },
  pdfNavButton: { display: "block", width: "100%", marginBottom: 2, padding: "5px 6px", background: "transparent",
                  color: "#9ab", border: 0, borderRadius: 4, textAlign: "left", cursor: "pointer", fontSize: 11,
                  overflow: "hidden", textOverflow: "ellipsis" },
  select: { flex: 1, minWidth: 120, padding: "4px 6px", background: "#0f1115", color: "#e8eaf0",
            border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  selectNarrow: { padding: "4px 6px", background: "#0f1115", color: "#e8eaf0",
                  border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  btn: { padding: "5px 14px", background: "#3b82f6", color: "#fff", border: "none",
         borderRadius: 6, cursor: "pointer", fontSize: 12 },
  result: { width: "100%", minHeight: 72, padding: 8, background: "#0f1115", color: "#e8eaf0",
            border: "1px solid #2a2f3a", borderRadius: 8, fontSize: 13,
            fontFamily: "Consolas, monospace", resize: "vertical" },
};
