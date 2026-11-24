import DOMPurify from "dompurify";
import { useCallback, useEffect, useRef, useState } from "react";
import { openProjectFile } from "../shell/openProjectFile";
import { beginFileOpen, useStore } from "../store";
import { resolveDocumentLink } from "../utils/documentLinks";
import { documentImageCandidates } from "../utils/documentSrcset";
import { fetchDocumentFile, readDocumentSnapshot } from "../utils/documentSnapshot";
import type { DocumentSnapshot } from "../utils/documentSnapshot";
import ReportHistory from "./ReportHistory";
import { publicationLabel, usePublicationStatus } from '../utils/publicationStatus';

interface Heading {
  id: string;
  text: string;
  level: number;
}

const DOCUMENT_STYLE = `
  article { max-width: 920px; margin: 0 auto; padding: 26px; background: #fff; color: #20242b;
            font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; overflow-wrap: anywhere; }
  h1, h2, h3, h4 { line-height: 1.3; margin: 1.25em 0 .55em; }
  h1 { font-size: 28px; } h2 { font-size: 23px; } h3 { font-size: 19px; }
  table { border-collapse: collapse; margin: 12px 0; max-width: 100%; }
  th, td { border: 1px solid #b9c0cc; padding: 5px 9px; }
  th { background: #eef1f5; } img, svg { max-width: 100%; height: auto; }
  pre { padding: 10px; overflow: auto; background: #f2f4f7; white-space: pre-wrap; }
  blockquote { margin-left: 0; padding-left: 14px; border-left: 3px solid #c5ccd6; color: #515a68; }
  a { color: #175cb5; cursor: pointer; }
  math[display="block"] { display: block; margin: 16px 0; overflow-x: auto; }
  .document-math-warning { color: #843900; background: #fff3df; padding: 2px 5px; }
`;

/** HTML/DOCX 共用安全语义阅读器：消毒、Blob 图片、heading 目录和算例链接。 */
export default function DocumentPanel() {
  const { activeFile, currentCase, user, activePublication, documentAnchor } = useStore();
  const hostRef = useRef<HTMLDivElement>(null);
  const [html, setHtml] = useState("");
  const [emptyDocument, setEmptyDocument] = useState(false);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [snapshot, setSnapshot] = useState<DocumentSnapshot | null>(null);
  const [reload, setReload] = useState(0);
  const selection = activePublication?.caseId === currentCase?.id && activePublication?.path === activeFile ? activePublication : null;
  const publicationId = selection?.id ?? undefined;
  const reportFile = activeFile === "E-report/build/report.html" || activeFile === "E-report/build/report.docx";
  const selectedSnapshot = !selection?.invalid && snapshot?.caseId === currentCase?.id && snapshot?.path === activeFile
    && (!publicationId || snapshot.publication?.id === publicationId) ? snapshot : null;
  const isDocx = activeFile?.toLowerCase().endsWith(".docx") ?? false;
  const publicationState = usePublicationStatus(user?.id, currentCase?.id, reportFile ? 'report' : null, selectedSnapshot?.publication ?? null);
  const readable = Boolean(selectedSnapshot && !publicationState.blocked);

  useEffect(() => {
    if (!activeFile || !currentCase) return;
    let cancelled = false;
    const controller = new AbortController();
    const objectUrls: string[] = [];
    const caseId = currentCase.id;
    const path = activeFile;
    setLoading(true);
    setSnapshot(null);
    setError("");
    setNotice("");
    setHtml("");
    setEmptyDocument(false);
    setHeadings([]);
    setWarnings([]);
    if (hostRef.current) hostRef.current.scrollTop = 0;
    (async () => {
      if (selection?.invalid) throw new Error('保存的版本选择无法核验，请明确选择所需发布。');
      const document = await readDocumentSnapshot(caseId, path, controller.signal, publicationId);
      let sourceHtml: string;
      let conversionWarnings: string[] = [];
      if (isDocx) {
        const { convertDocxWithMath } = await import('../utils/docxMath');
        const result = await convertDocxWithMath(await document.original.arrayBuffer()).catch(() => {
          throw new Error('无法预览这份 DOCX。请确认文件可在办公软件中打开，重新上传后点击“重新读取”；报告文件可重试“重读所选版本”。');
        });
        sourceHtml = result.value;
        conversionWarnings = result.warnings;
      } else {
        sourceHtml = await document.original.text();
      }
      const sanitized = DOMPurify.sanitize(sourceHtml, {
        USE_PROFILES: { html: true, svg: true, mathMl: true },
        FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "input", "button",
                      "textarea", "select", "link", "meta", "base"],
        FORBID_ATTR: ["style", "srcdoc"],
        // A srcset is a list, not one URI. Every candidate is checked and
        // replaced below before this detached document is mounted.
        ADD_URI_SAFE_ATTR: ['srcset'],
      });
      const parsed = new DOMParser().parseFromString(sanitized, "text/html");
      // Pandoc embeds local SVG figures as inline markup. Keep their geometry,
      // while refusing references that could fetch live content into a snapshot.
      for (const node of parsed.querySelectorAll('svg image, svg use')) {
        const reference = node.getAttribute('href') ?? node.getAttribute('xlink:href') ?? '';
        if (reference && !reference.startsWith('#')
            && !(node.localName === 'image' && /^data:image\/(?:png|jpe?g|gif|webp|bmp|svg\+xml);base64,/i.test(reference))) {
          node.replaceWith(parsed.createTextNode('[外部或未封存的 SVG 资源未加载]'));
        }
      }
      for (const svg of parsed.querySelectorAll('svg')) {
        svg.setAttribute('role', 'img');
        if (!svg.hasAttribute('aria-label') && !svg.hasAttribute('aria-labelledby')) {
          svg.setAttribute('aria-label', svg.getAttribute('alt')
            || svg.closest('figure')?.querySelector('figcaption')?.textContent || '报告图表');
        }
      }
      const imageUrls = new Map<string, Promise<string>>();
      const resolveImage = (source: string): Promise<string> => {
        if (/^data:image\/(?:png|jpe?g|gif|webp|bmp|avif|svg\+xml);base64,/i.test(source)) return Promise.resolve(source);
        const cached = imageUrls.get(source);
        if (cached) return cached;
        const resolved = resolveDocumentLink(path, source);
        if (resolved.kind !== 'case') return Promise.reject(new Error('外部或无效图片未加载'));
        // Published candidates must be embedded, never replaced by live files.
        if (document.publication) return Promise.reject(new Error('图片未包含在此报告版本中'));
        const pending = (async () => {
          const imageResponse = await fetchDocumentFile(caseId, resolved.path, controller.signal);
          const blob = await imageResponse.blob();
          if (!blob.type.startsWith('image/') && !/\.(?:png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(resolved.path)) {
            throw new Error('不是支持的图片文件');
          }
          const url = URL.createObjectURL(blob);
          if (cancelled) {
            URL.revokeObjectURL(url);
            throw new Error('图片读取已取消');
          }
          objectUrls.push(url);
          return url;
        })();
        imageUrls.set(source, pending);
        return pending;
      };
      await Promise.all([...parsed.querySelectorAll('img[srcset], source[srcset]')].map(async image => {
        const original = image.getAttribute('srcset') ?? '';
        image.removeAttribute('srcset');
        try {
          const candidates = documentImageCandidates(original);
          const values = await Promise.all(candidates.map(async ({ url, descriptor }) =>
            (await resolveImage(url)) + (descriptor ? ' ' + descriptor : '')));
          if (!cancelled) image.setAttribute('srcset', values.join(', '));
        } catch (cause: any) {
          if (!cancelled) conversionWarnings.push(`候选图片未加载：${cause?.message ?? String(cause)}`);
        }
      }));
      await Promise.all([...parsed.querySelectorAll('img')].map(async image => {
        const source = image.getAttribute('src') ?? '';
        if (!source && (image.hasAttribute('srcset') || image.closest('picture')?.querySelector('source[srcset]'))) return;
        try {
          image.src = await resolveImage(source);
          image.setAttribute('loading', 'lazy');
        } catch (cause: any) {
          if (!cancelled) image.replaceWith(parsed.createTextNode(`[图片读取失败：${cause?.message ?? String(cause)}]`));
        }
      }));
      if (cancelled) return;
      const found = [...parsed.querySelectorAll("h1, h2, h3, h4")].map((element, index) => {
        if (!element.id) element.id = `vf-document-heading-${index}`;
        return { id: element.id, text: element.textContent?.trim() || `第 ${index + 1} 节`,
                 level: Number(element.tagName.slice(1)) };
      });
      if (document.publication && user) useStore.getState().rememberPublication(user.id, caseId, path, publicationId ?? null, document.publication.id);
      setHeadings(found);
      setWarnings(conversionWarnings);
      setEmptyDocument(!parsed.body.textContent?.trim()
        && !parsed.body.querySelector("img, svg, math, table, video, audio, hr"));
      setHtml(parsed.body.innerHTML);
      setSnapshot(document);
      setLoading(false);
    })().catch((cause: any) => {
      if (!cancelled) {
        setError(cause?.message ?? String(cause));
        setLoading(false);
      }
    });
    return () => {
      cancelled = true;
      controller.abort();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [activeFile, currentCase?.id, user?.id, isDocx, reload, publicationId, selection?.invalid]);

  const jumpTo = useCallback((id: string) => {
    let element = hostRef.current?.shadowRoot?.getElementById(id);
    if (!element) {
      setNotice(`未找到文档锚点：${id}`);
      return;
    }
    // Mammoth represents Word heading bookmarks as an empty preceding anchor.
    // Focus the readable heading rather than an invisible zero-size marker.
    if (element.localName === 'a' && !element.textContent?.trim()
        && element.nextElementSibling?.matches('h1, h2, h3, h4, h5, h6')) {
      element = element.nextElementSibling as HTMLElement;
    }
    // Headings and footnote targets are not necessarily keyboard focusable.
    if (element instanceof HTMLElement) {
      if (!element.hasAttribute('tabindex')) element.setAttribute('tabindex', '-1');
      element.focus({ preventScroll: true });
    }
    element.scrollIntoView({ block: "start" });
    setNotice(`已定位 ${element.textContent?.trim().slice(0, 60) || id}`);
  }, []);

  const openLink = useCallback((href: string) => {
    if (!activeFile) return;
    const resolved = resolveDocumentLink(activeFile, href);
    if (resolved.kind === "internal") {
      try { jumpTo(decodeURIComponent(resolved.hash)); }
      catch { setNotice("锚点编码无效"); }
    } else if (resolved.kind === "external") {
      window.open(resolved.url, "_blank", "noopener,noreferrer");
    } else if (resolved.kind === "case") {
      let hash: string;
      try { hash = decodeURIComponent(resolved.hash); }
      catch { setNotice("锚点编码无效"); return; }
      const intent = beginFileOpen();
      if (!intent) return;
      void openProjectFile(resolved.path, {hash, intent}).catch((cause) => {
        if (intent.isCurrent()) setNotice(`链接打开失败。${cause?.message ?? String(cause)}`);
      });
    } else {
      setNotice("已阻止无效或不安全的文档链接");
    }
  }, [activeFile, currentCase?.id, user?.id, jumpTo]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    shadow.innerHTML = "";
    if (!html || !readable) return;
    const style = document.createElement("style");
    style.textContent = DOCUMENT_STYLE;
    const article = document.createElement("article");
    article.setAttribute("data-testid", "document-content");
    article.innerHTML = html;
    shadow.append(style, article);
    const click = (event: Event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest("a");
      if (!anchor) return;
      event.preventDefault();
      openLink(anchor.getAttribute("href") ?? "");
    };
    shadow.addEventListener("click", click);
    return () => shadow.removeEventListener("click", click);
  }, [html, openLink, readable]);

  useEffect(() => {
    // ViewerPanel remounts for another file. Consume the navigation only after
    // its own snapshot has rendered, including asynchronous DOCX conversion.
    if (!documentAnchor || documentAnchor.caseId !== currentCase?.id || documentAnchor.path !== activeFile) return;
    if (!loading && (error || publicationState.blocked)) {
      useStore.getState().clearDocumentAnchor(documentAnchor.requestId);
    } else if (readable && !loading && hostRef.current?.shadowRoot?.querySelector('article')) {
      jumpTo(documentAnchor.hash);
      useStore.getState().clearDocumentAnchor(documentAnchor.requestId);
    }
  }, [documentAnchor, currentCase?.id, activeFile, loading, error, publicationState.blocked, readable, html, jumpTo]);

  const downloadOriginal = async () => {
    if (!selectedSnapshot || loading || error || publicationState.blocked) return;
    setNotice("下载中…");
    try {
      const url = URL.createObjectURL(selectedSnapshot.original);
      try {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = selectedSnapshot.path.split("/").pop() || "download";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      setNotice("已开始下载当前显示版本的原件");
    } catch (cause: any) {
      setNotice(cause?.message ?? String(cause));
    }
  };

  if (!activeFile) return <div style={s.hint}>未选择文件</div>;
  return (
    <div data-testid="document-reader" data-kind={isDocx ? "docx" : "html"} data-file={activeFile} style={s.page}>
      <div style={s.bar} className="publication-reading-actions">
        <b>{isDocx ? "DOCX" : "HTML"} 安全阅读器</b>
        <span style={{ color: "#8b93a7", fontSize: 11 }}>语义内容 · 不执行脚本/宏 · 保留原件</span>
        {warnings.length > 0 && <span style={{ color: "#d8b86c", fontSize: 11 }}>{warnings.length} 条转换提示</span>}
        <button data-testid="document-reload" style={s.download} disabled={loading || selection?.invalid}
                onClick={() => {
                  if (user && currentCase && reportFile && !selection?.invalid) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, selectedSnapshot?.publication?.id ?? publicationId ?? null);
                  setReload(value => value + 1);
                }}>{reportFile ? '重读所选版本' : '重新读取'}</button>
        {reportFile && <button data-testid="document-latest" style={{ ...s.download, marginLeft: 0 }} disabled={loading}
          onClick={() => { if (user && currentCase) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, null); setReload(value => value + 1); }}>打开最新发布</button>}
        <button data-testid="document-download" style={{ ...s.download, marginLeft: 0 }}
                disabled={loading || !!error || !readable} onClick={() => { void downloadOriginal(); }}>下载</button>
      </div>
      {reportFile && currentCase && <ReportHistory key={`${currentCase.id}:${activeFile}`}
        caseId={currentCase.id} selectedId={selectedSnapshot?.publication?.id}
        onSelect={id => { if (user) useStore.getState().selectPublication(user.id, currentCase.id, activeFile, id); setReload(value => value + 1); }} />}
      {selectedSnapshot && <div data-testid="document-version" role="status" style={s.notice}>
        {selectedSnapshot.publication
          ? publicationState.error || `报告${publicationLabel(publicationState.publication ?? selectedSnapshot.publication)}`
          : "工作区文件 · 下载保留打开时的原件"}
      </div>}
      <div style={s.body}>
        <aside style={s.toc}>
          <div style={s.tocTitle}>文档导航</div>
          {readable && headings.map((heading) => (
            <button key={heading.id} data-testid="document-heading" data-target={heading.id}
                    onClick={() => jumpTo(heading.id)}
                    style={{ ...s.heading, paddingLeft: 8 + (heading.level - 1) * 10 }}>
              {heading.text}
            </button>
          ))}
          {!loading && readable && !headings.length && <div style={s.hint}>无 heading 目录</div>}
        </aside>
        <div style={s.view}>
          {loading && <div style={s.hint}>正在解析文档…</div>}
          {error && <div role="alert" style={s.error}>{error}</div>}
          {publicationState.blocked && <div role="alert" style={s.error}>{publicationState.error}</div>}
          {notice && <div data-testid="document-notice" style={s.notice}>{notice}</div>}
          {!loading && !error && readable && emptyDocument && <p data-testid="document-empty" role="status" style={s.empty}>
            所选文档没有可显示的正文。可下载原件，或选择其他发布版本。
          </p>}
          <div ref={hostRef} data-testid="document-shadow" style={s.document} />
        </div>
      </div>
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  page: { height: "100%", display: "flex", flexDirection: "column", minHeight: 0 },
  bar: { display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", padding: "7px 10px", borderBottom: "1px solid #2a2f3a", fontSize: 12 },
  body: { flex: 1, display: "flex", minHeight: 0 },
  toc: { width: 210, flexShrink: 0, overflow: "auto", borderRight: "1px solid #2a2f3a", padding: 6 },
  tocTitle: { color: "var(--vf-muted)", fontSize: 14, padding: "5px 7px" },
  heading: { display: "block", width: "100%", minHeight: 44, border: 0, background: "transparent", color: "var(--vf-text)",
             textAlign: "left", padding: "6px 8px", cursor: "pointer", fontSize: 14 },
  view: { flex: 1, minWidth: 0, display: "flex", flexDirection: "column", background: "#0b0d12" },
  document: { flex: 1, minHeight: 0, overflow: "auto" },
  hint: { color: "var(--vf-muted)", padding: 10, fontSize: 14 },
  empty: { margin: 0, padding: 16, fontSize: 14, lineHeight: 1.6, color: "var(--vf-text)", background: "var(--vf-panel)" },
  error: { color: "var(--vf-text)", padding: 8, fontSize: 14 },
  notice: { color: "var(--vf-text)", padding: "6px 10px", fontSize: 14, lineHeight: 1.6, overflowWrap: 'anywhere', borderBottom: "1px solid #2a2f3a" },
  download: { marginLeft: "auto", minHeight: 44, padding: "6px 12px", background: "var(--vf-panel)", color: "var(--vf-text)", border: "1px solid var(--vf-border)", borderRadius: 5, cursor: "pointer", fontSize: 14 },
};
