import DOMPurify from "dompurify";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { prepareMarkdownPreview } from "../utils/markdownMath";
import "./MarkdownPreview.css";

type CompatibilityResult = { source: string; attempt: number; html?: string; unavailable?: boolean };
type ImportedAssets = { caseId: number; documentPath: string };

export default function MarkdownPreview({ source, style, assets }: { source: string; style: CSSProperties; assets?: ImportedAssets }) {
  const prepared = useMemo(() => {
    const preview = prepareMarkdownPreview(source);
    return { ...preview, html: DOMPurify.sanitize(preview.html) };
  }, [source]);
  const [result, setResult] = useState<CompatibilityResult | null>(null);
  const [attempt, setAttempt] = useState(0);
  const current = result?.source === source && result.attempt === attempt ? result : null;
  const busy = prepared.compatibility && !current;
  const rendered = useRef<HTMLDivElement | null>(null);
  const assetCase = assets?.caseId;
  const assetDirectory = assets?.documentPath.slice(0, assets.documentPath.lastIndexOf("/") + 1);
  const html = useMemo(() => {
    const content = current?.html ?? prepared.html;
    if (!assetCase || !assetDirectory) return content;
    const doc = new DOMParser().parseFromString(content, "text/html");
    for (const image of doc.querySelectorAll("img[src]")) {
      const path = image.getAttribute("src") ?? "";
      if (/^assets\/[0-9a-f]{64}\.(png|jpe?g|gif|webp)$/i.test(path)) {
        image.setAttribute("data-case-image", assetDirectory + path);
        image.removeAttribute("src");
      }
    }
    return doc.body.innerHTML;
  }, [current?.html, prepared.html, assetCase, assetDirectory]);

  useEffect(() => {
    if (!assetCase || !rendered.current) return;
    const controller = new AbortController();
    const urls: string[] = [];
    const token = localStorage.getItem("vf_token");
    for (const image of rendered.current.querySelectorAll<HTMLImageElement>("img[data-case-image]")) {
      const path = image.dataset.caseImage;
      if (!path) continue;
      void fetch(`/api/v1/cases/${assetCase}/raw?path=${encodeURIComponent(path)}`,
        { signal: controller.signal, credentials: "same-origin", headers: token ? { Authorization: `Bearer ${token}` } : {} })
        .then(async response => {
          if (!response.ok) return;
          const blob = await response.blob();
          if (controller.signal.aborted) return;
          const url = URL.createObjectURL(blob);
          urls.push(url);
          image.src = url;
        }).catch(() => {});
    }
    return () => { controller.abort(); urls.forEach(url => URL.revokeObjectURL(url)); };
  }, [assetCase, html]);

  useEffect(() => {
    if (!prepared.compatibility) return;
    let disposed = false;
    void import("../utils/markdownMathCompatibility").then(({ renderCompatibleMarkdown }) => {
      if (disposed) return;
      const html = DOMPurify.sanitize(renderCompatibleMarkdown(prepared.html), {
        ADD_TAGS: ["mjx-container", "mjx-assistive-mml"],
        ADD_ATTR: ["jax", "display", "focusable"],
      });
      setResult({ source, attempt, html });
    }, () => {
      if (!disposed) setResult({ source, attempt, unavailable: true });
    }).catch(() => {
      if (!disposed) setResult({ source, attempt });
    });
    return () => { disposed = true; };
  }, [attempt, prepared, source]);

  return <div className="md-preview" data-testid="markdown-preview" style={style} aria-busy={busy}>
    {busy && <p className="markdown-math-status" role="status">正在排版公式…</p>}
    {prepared.compatibility && current && current.html === undefined && <div className="markdown-math-status" role="alert">
      {current.unavailable ? "本地公式组件未加载，正文与公式源码仍可查看。" : "公式排版失败，正文与公式源码仍可查看。"}
      {!current.unavailable && <button type="button" onClick={() => setAttempt(value => value + 1)}>重试排版</button>}
    </div>}
    <div ref={rendered} dangerouslySetInnerHTML={{ __html: html }} />
  </div>;
}
