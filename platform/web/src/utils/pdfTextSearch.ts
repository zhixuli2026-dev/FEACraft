export interface PdfTextItem {
  str: string;
  hasEOL?: boolean;
}

export interface PdfTextContent {
  items: PdfTextItem[];
  styles: Record<string, unknown>;
}

interface TextPosition { item: number; start: number; end: number }
interface TextMatch { start: number; end: number }

const normalized = (text: string) => text.normalize("NFKD").toLowerCase();
export const pdfSearchQuery = (text: string) => normalized(text).replace(/\s+/gu, " ").trim();

/** Joins PDF.js text runs without inserting spaces between split glyph runs. */
function indexText(items: PdfTextItem[], withPositions: boolean) {
  let text = "";
  let space = true;
  const positions: Array<TextPosition | null> = [];
  const append = (value: string, position: TextPosition | null) => {
    if (/^\s$/u.test(value)) {
      if (space) return;
      value = " ";
      space = true;
    } else {
      space = false;
    }
    text += value;
    if (withPositions) for (let n = 0; n < value.length; n++) positions.push(position);
  };
  for (const [item, run] of items.entries()) {
    let offset = 0;
    for (const glyph of run.str) {
      const end = offset + glyph.length;
      const position = withPositions ? { item, start: offset, end } : null;
      for (const value of normalized(glyph)) append(value, position);
      offset = end;
    }
    if (run.hasEOL) append(" ", null);
  }
  return { text, positions };
}

function offsets(text: string, query: string): TextMatch[] {
  const matches: TextMatch[] = [];
  if (!query) return matches;
  for (let start = text.indexOf(query); start >= 0; start = text.indexOf(query, start + query.length)) {
    matches.push({ start, end: start + query.length });
  }
  return matches;
}

export function pdfPageSearch(items: PdfTextItem[], query: string) {
  const { text } = indexText(items, false);
  const matches = offsets(text, query);
  const match = matches[0];
  return match ? { count: matches.length, snippet: text.slice(Math.max(0, match.start - 40), match.end + 65).normalize("NFC") } : null;
}

export interface PdfHighlight { x: number; y: number; width: number; height: number }

/** Selects source offsets on each positioned span, leaving actual text selection untouched. */
export function pdfHighlightRects(items: PdfTextItem[], textDivs: HTMLElement[], host: HTMLElement, query: string) {
  const { text, positions } = indexText(items, true);
  const frame = host.getBoundingClientRect();
  const rectangles: PdfHighlight[] = [];
  for (const { start, end } of offsets(text, query)) {
    const slices = new Map<number, TextPosition>();
    for (let offset = start; offset < end; offset++) {
      const position = positions[offset];
      if (!position) continue;
      const previous = slices.get(position.item);
      slices.set(position.item, previous ? { ...previous, end: position.end } : position);
    }
    for (const slice of slices.values()) {
      const node = textDivs[slice.item]?.firstChild;
      if (!(node instanceof Text) || slice.end > node.length) continue;
      const range = document.createRange();
      range.setStart(node, slice.start);
      range.setEnd(node, slice.end);
      for (const bounds of range.getClientRects()) {
        if (bounds.width > 0 && bounds.height > 0) rectangles.push({
          x: bounds.left - frame.left, y: bounds.top - frame.top, width: bounds.width, height: bounds.height,
        });
      }
    }
  }
  return rectangles;
}

/** Consume the actual cancellable PDF.js worker text stream. */
export async function readPdfText(page: { streamTextContent(): ReadableStream<PdfTextContent> }, signal: AbortSignal) {
  signal.throwIfAborted();
  const reader = page.streamTextContent().getReader();
  const stop = () => { void reader.cancel(new DOMException("PDF text read cancelled", "AbortError")).catch(() => {}); };
  signal.addEventListener("abort", stop, { once: true });
  const content: PdfTextContent = { items: [], styles: Object.create(null) };
  try {
    for (;;) {
      const next = await reader.read();
      signal.throwIfAborted();
      if (next.done) return content;
      content.items.push(...next.value.items);
      Object.assign(content.styles, next.value.styles);
    }
  } finally {
    signal.removeEventListener("abort", stop);
    reader.releaseLock();
  }
}
