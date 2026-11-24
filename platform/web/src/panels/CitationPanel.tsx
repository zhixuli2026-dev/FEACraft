import { useEffect, useRef, useState, type CSSProperties } from "react";
import DOMPurify from "dompurify";
import { api, ApiError } from "../api";
import { captureProjectCase, useStore } from "../store";
import { activatePanel } from "../shell/dock";
import { captureEditorInsertion } from "../shell/editorRef";
import { formatCitation, type CitationItem, type CitationStyle } from "../utils/citationStyle";
import "./CitationPanel.css";

const bibliography = "E-report/latex/refs.bib";
const bibliographyQuery = `?path=${encodeURIComponent(bibliography)}`;
type SearchResult = CitationItem & { key_hint: string; bibtex: string };
type Reference = { key: string; title: string; identity?: string };
type Snapshot = { content: string; content_sha256: string | null };
type PreviewStyle = CitationStyle | "custom";
type Preview = { pending?: boolean; html?: string; error?: string };

async function readStyle(file: File | undefined) {
  if (!file) return undefined;
  if (file.size > 512 * 1024) throw new Error("CSL 样式文件须小于 512 KiB。");
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch {
    throw new Error("无法读取 CSL 样式，请选择 UTF-8 编码的 .csl 文件。");
  }
}

function identityOf(value: string) {
  const identity = value.toLowerCase();
  if (identity.startsWith("arxiv:")) return identity.replace(/v\d+$/, "");
  if (!identity.startsWith("doi:")) return identity;
  const doi = identity.slice(4).replace(/^(?:doi:\s*|https?:\/\/(?:dx\.)?doi\.org\/)/, "").trim();
  try { return "doi:" + decodeURIComponent(doi).toLowerCase(); }
  catch { return "doi:" + doi; }
}

function readReferences(source: string): Reference[] {
  const starts = [...source.matchAll(/^[ \t]*@(\w+)\s*[{(]/gm)];
  return starts.flatMap((entry, index) => {
    if (/^(?:comment|string|preamble)$/i.test(entry[1])) return [];
    const body = source.slice(entry.index + entry[0].length, starts[index + 1]?.index);
    const key = body.match(/^\s*([\w:./+\-]+)\s*,/)?.[1];
    if (!key) return [];
    const field = (name: string) => body.match(new RegExp(`(?:^|,)\\s*${name}\\s*=\\s*[{"]([^}\"]+)`, "i"))?.[1].trim();
    const doi = field("doi");
    const eprint = field("eprint");
    const note = field("note");
    const identifier = doi ? `doi:${doi}` : eprint && /^arxiv$/i.test(field("archivePrefix") ?? "") ? `arXiv:${eprint}`
      : note && /^semanticscholar:[a-f0-9]{40}$/i.test(note) ? note : undefined;
    return [{ key, title: field("title") ?? "", identity: identifier && identityOf(identifier) }];
  });
}

async function readBibliography(caseId: number): Promise<Snapshot> {
  try {
    const data = await api.get<Snapshot>(`/cases/${caseId}/file${bibliographyQuery}`);
    if (typeof data.content !== "string" || !data.content_sha256) throw new Error("无法确认文献库版本。");
    return data;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.detail === "文件不存在") {
      return { content: "", content_sha256: null };
    }
    throw error;
  }
}

function entryFor(item: SearchResult, refs: Reference[]) {
  const identity = identityOf(item.id);
  const match = refs.find(ref => ref.identity === identity);
  if (match) return { key: match.key };
  if (!/^\w[\w:.-]*$/.test(item.key_hint)) throw new Error("检索结果没有可用的引用键。");
  const keys = new Set(refs.map(ref => ref.key.toLowerCase()));
  let key = item.key_hint;
  for (let suffix = 2; keys.has(key.toLowerCase()); ++suffix) key = `${item.key_hint}-${suffix}`;
  const header = item.bibtex.match(/^(@\w+\s*[{(]\s*)([\w:.-]+)(\s*,)/);
  if (header?.[2] !== item.key_hint) throw new Error("检索结果无法写入 BibTeX。");
  return { key, addition: `${header[1]}${key}${header[3]}${item.bibtex.slice(header[0].length)}` };
}

export default function CitationPanel() {
  const scopeId = useStore(state => state.currentCase?.id);
  const userId = useStore(state => state.user?.id);
  const refsVersion = useStore(state => {
    const entry = state.fileTree.find(file => file.path === bibliography);
    return entry && `${entry.modified}:${entry.size}`;
  });
  const [refs, setRefs] = useState<Reference[]>([]);
  const [refsError, setRefsError] = useState("");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("crossref");
  const [style, setStyle] = useState<PreviewStyle>("apa");
  const [csl, setCsl] = useState<File>();
  const [parentCsl, setParentCsl] = useState<File>();
  const [preview, setPreview] = useState<Preview>();
  const previewVersion = useRef(0);
  const parentPicker = useRef<HTMLInputElement>(null);
  const [online, setOnline] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [adding, setAdding] = useState(false);
  const [feedback, setFeedback] = useState<{ message: string; error?: boolean }>();
  const writeLock = useRef(false);

  useEffect(() => {
    setCsl(undefined);
    setParentCsl(undefined);
    setPreview(undefined);
    setRefs([]);
    setRefsError("");
    return () => { previewVersion.current += 1; };
  }, [scopeId, userId]);

  useEffect(() => {
    if (!scopeId) return;
    const selection = captureProjectCase();
    let active = true;
    readBibliography(scopeId).then(snapshot => {
      if (active && selection?.isCurrent()) {
        setRefs(readReferences(snapshot.content));
        setRefsError("");
      }
    }).catch(error => {
      if (active && selection?.isCurrent()) setRefsError(`文献库读取失败：${error.message}`);
    });
    return () => { active = false; };
  }, [scopeId, refsVersion, userId]);

  const clearPreview = () => {
    previewVersion.current += 1;
    setPreview(undefined);
  };

  const previewCitation = async (localKeys: string[], item?: SearchResult) => {
    const selection = captureProjectCase();
    if (!selection) return;
    const version = ++previewVersion.current;
    const current = () => version === previewVersion.current && selection.isCurrent();
    setPreview({ pending: true });
    try {
      const [styleFile, parentFile] = style === "custom"
        ? await Promise.all([readStyle(csl), readStyle(parentCsl)]) : [];
      if (style === "custom" && !styleFile) throw new Error("请先选择 CSL 样式文件。");
      const bibtex = item ? item.bibtex : (await readBibliography(selection.caseId)).content;
      if (!current()) return;
      const response = await api.post<{ html: string }>(`/cite/cases/${selection.caseId}/format`, {
        bibtex, preset: style, style: styleFile, parent: parentFile, keys: item ? [item.key_hint] : localKeys,
      });
      if (!current()) return;
      const html = DOMPurify.sanitize(response.html, {
        ALLOWED_TAGS: ["a", "br", "div", "em", "i", "li", "ol", "p", "section", "small", "span", "strong", "sub", "sup", "ul"],
        ALLOWED_ATTR: ["href", "class", "id", "role", "aria-label"],
        ALLOW_DATA_ATTR: false,
        ALLOWED_URI_REGEXP: /^(?:https?:\/\/|#)/i,
      });
      if (!html.trim()) throw new Error("这个样式没有为所选条目生成内容。");
      setPreview({ html });
    } catch (error) {
      if (current()) setPreview({ error: error instanceof Error ? error.message : String(error) });
    }
  };

  const search = async () => {
    const terms = query.trim();
    if (terms.length < 2 || searching) return;
    setSearching(true);
    setSearchError("");
    setOnline([]);
    try {
      const found = await api.get<{ items: SearchResult[] }>(`/cite/search?${new URLSearchParams({ q: terms, source })}`);
      setOnline(found.items);
      if (!found.items.length) setSearchError("未找到匹配文献。");
    } catch (error) {
      setSearchError(`文献检索失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSearching(false);
    }
  };

  const editorTarget = () => {
    const { currentWs, activeFile, openFiles } = useStore.getState();
    if (currentWs?.access_role === "viewer") throw new Error("当前工作区为只读。");
    if (!activeFile || activeFile === bibliography || openFiles[activeFile]?.kind !== "text") {
      throw new Error("请先打开要插入引用的正文源文件。");
    }
    activatePanel("editor");
    const insert = captureEditorInsertion(activeFile);
    if (!insert) throw new Error("请在编辑器的源码视图中定位光标后重试。");
    return insert;
  };

  const citeLocal = (key: string) => {
    try {
      if (!editorTarget()(`\\cite{${key}}`)) throw new Error("编辑器已变化，请重试。");
      setFeedback({ message: `已插入 ${key}，请保存正文。` });
    } catch (error) {
      setFeedback({ error: true, message: error instanceof Error ? error.message : String(error) });
    }
  };

  const citeOnline = async (item: SearchResult) => {
    if (writeLock.current) return;
    const selection = captureProjectCase();
    if (!selection) return;
    writeLock.current = true;
    setAdding(true);
    setFeedback(undefined);
    try {
      const insert = editorTarget();
      const openLibrary = () => useStore.getState().openFiles[bibliography];
      if (openLibrary()?.dirty || openLibrary()?.saving) throw new Error("请先保存或关闭正在编辑的 refs.bib。");
      const snapshot = await readBibliography(selection.caseId);
      if (!selection.isCurrent()) return;
      const entry = entryFor(item, readReferences(snapshot.content));
      const open = openLibrary();
      if (open?.dirty || open?.saving) throw new Error("请先保存或关闭正在编辑的 refs.bib。");
      if (entry.addition) {
        const join = !snapshot.content || snapshot.content.endsWith("\n\n") ? "" : snapshot.content.endsWith("\n") ? "\n" : "\n\n";
        const content = `${snapshot.content}${join}${entry.addition}\n`;
        await api.put(`/cases/${selection.caseId}/file`, {
          path: bibliography, content, expected_sha256: snapshot.content_sha256,
        });
        if (!selection.isCurrent()) return;
        setRefs(readReferences(content));
        void useStore.getState().loadTree().catch(() => {});
      } else {
        setRefs(readReferences(snapshot.content));
      }
      if (!insert(`\\cite{${entry.key}}`)) {
        setFeedback({ message: `文献库已收录 ${entry.key}。光标或正文已变化，请从本地条目插入。` });
        return;
      }
      setFeedback({ message: `已录入并插入 ${entry.key}，请保存正文。` });
    } catch (error) {
      if (selection.isCurrent()) {
        const detail = error instanceof Error ? error.message : String(error);
        setFeedback({ error: true, message: `引用未插入：${detail}` });
      }
    } finally {
      writeLock.current = false;
      setAdding(false);
    }
  };

  return <section aria-label="引用" style={{ borderTop: "1px solid var(--vf-border)", paddingTop: 8 }}>
    <div style={{ ...row, flexWrap: "wrap" }}>
      <label htmlFor="vf-citation-style">引用预览格式</label>
      <select id="vf-citation-style" data-testid="cite-style" style={control} value={style}
              onChange={event => {
                setStyle(event.target.value as PreviewStyle);
                setCsl(undefined);
                setParentCsl(undefined);
                clearPreview();
              }}>
        <option value="apa">APA</option>
        <option value="ieee">IEEE</option>
        <option value="chicago">Chicago（作者年份）</option>
        <option value="custom">自定义 CSL</option>
      </select>
    </div>
    {style === "custom" && <div key={`${scopeId}:${userId}`} className="vf-citation-files" style={{ paddingBlock: 3 }}>
      <label style={filePicker}>CSL 样式文件
        <input type="file" accept=".csl" style={{ maxWidth: "100%" }}
               onChange={event => { setCsl(event.target.files?.[0]); clearPreview(); }} />
      </label>
      <label style={filePicker}>父样式文件（有依赖时）
        <input ref={parentPicker} type="file" accept=".csl" style={{ maxWidth: "100%" }}
               onChange={event => { setParentCsl(event.target.files?.[0]); clearPreview(); }} />
      </label>
      {parentCsl && <button type="button" style={button} onClick={() => {
        if (parentPicker.current) parentPicker.current.value = "";
        setParentCsl(undefined);
        clearPreview();
      }}>移除父样式</button>}
    </div>}
    {preview?.pending && <p role="status">正在排版引文…</p>}
    {preview?.error && <div role="alert" data-testid="cite-format-error" className="vf-project-error">{preview.error}</div>}
    {preview?.html && <div role="region" aria-label="排版结果" data-testid="cite-formatted" className="vf-citation-format"
                          dangerouslySetInnerHTML={{ __html: preview.html }} />}
    <div style={{ color: "var(--vf-muted)", marginBottom: 4 }}>
      本地文献（refs.bib{refs.length ? ` · ${refs.length} 条` : " 暂无条目"}）
      {refs.length > 1 && refs.length <= 100 && <button type="button" style={{ ...button, marginLeft: 6 }}
        onClick={() => void previewCitation(refs.map(ref => ref.key))}>预览书目</button>}
    </div>
    {refsError && <div role="alert" className="vf-project-error">{refsError}</div>}
    {refs.map(ref => <div key={ref.key} data-testid="cite-local-item" style={row}>
      <code>{ref.key}</code>
      <span style={title} title={ref.title}>{ref.title}</span>
      <button type="button" style={button} aria-label={`预览 ${ref.key}`}
              onClick={() => void previewCitation([ref.key])}>预览</button>
      <button type="button" style={button} title={`\\cite{${ref.key}}`} disabled={adding} onClick={() => citeLocal(ref.key)}>插入</button>
    </div>)}
    <form style={{ ...row, flexWrap: "wrap", marginTop: 6 }} onSubmit={event => { event.preventDefault(); void search(); }}>
      <input data-testid="cite-query" aria-label="在线检索文献" style={{ ...control, flex: "1 1 150px" }}
             placeholder="题名、关键词、DOI 或文献编号" value={query} onChange={event => setQuery(event.target.value)} />
      <select data-testid="cite-source" aria-label="文献来源" style={control} disabled={searching}
              value={source} onChange={event => { setSource(event.target.value); setOnline([]); setSearchError(""); }}>
        <option value="crossref">Crossref</option>
        <option value="arxiv">arXiv</option>
        <option value="semanticscholar">Semantic Scholar</option>
      </select>
      <button data-testid="cite-search-run" type="submit" style={button} disabled={searching || query.trim().length < 2}>
        {searching ? "检索中…" : "检索"}
      </button>
    </form>
    {searchError && <div data-testid="cite-error" role="status" className="vf-project-error">{searchError}</div>}
    {feedback && <div data-testid="cite-feedback" role={feedback.error ? "alert" : "status"} className="vf-project-error">{feedback.message}</div>}
    {online.map((item, index) => <div key={item.id} data-testid="cite-online-item" data-citation-id={item.id}>
      <div style={row}>
        <code>{item.key_hint}</code>
        <span style={title} title={item.title}>{item.title}（{item.year}）</span>
        <button type="button" style={button} aria-label={`预览 ${item.key_hint}`}
                onClick={() => void previewCitation([], item)}>预览</button>
        <button data-testid="cite-add" type="button" style={button} disabled={adding} onClick={() => void citeOnline(item)}>引+录</button>
      </div>
      {style !== "custom" && <div data-testid="cite-preview" style={{ color: "var(--vf-muted)", fontSize: 12, overflowWrap: "anywhere" }}>
        {formatCitation(item, style, index + 1)}
      </div>}
    </div>)}
  </section>;
}

const row: CSSProperties = { display: "flex", gap: 6, alignItems: "center", paddingBlock: 3 };
const title: CSSProperties = { color: "var(--vf-muted)", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const control: CSSProperties = { minWidth: 0, border: "1px solid var(--vf-border)", borderRadius: 5,
  background: "var(--vf-bg)", color: "var(--vf-text)", padding: "4px 8px" };
const button: CSSProperties = { ...control, cursor: "pointer", flexShrink: 0 };
const filePicker: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", marginBottom: 5 };
