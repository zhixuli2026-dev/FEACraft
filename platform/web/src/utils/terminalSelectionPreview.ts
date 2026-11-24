import DOMPurify from "dompurify";
import katex from "katex";
import { Marked, type TokenizerAndRendererExtension } from "marked";

export const terminalPreviewLimit = 32 * 1024;
const formulaLimit = 2048;
const display = /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])/;
const inline = /^(?:\$(?!\$)([^$\n]+?)\$(?!\$)|\\\(([^\n]+?)\\\))/;

function escaped(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function passive(html: string): string {
  return DOMPurify.sanitize(html, { FORBID_TAGS: ["a", "img"], FORBID_ATTR: ["href", "src", "srcset"] });
}

function formula(text: string, displayMode: boolean): string | null {
  if (text.length > formulaLimit) return null;
  try {
    return katex.renderToString(text, { displayMode, throwOnError: true, trust: false, maxExpand: 128, maxSize: 24 });
  } catch {
    return null;
  }
}

export function renderTerminalSelection(text: string, mode: "markdown" | "latex"): { html: string; invalid: boolean } {
  if (mode === "latex") {
    const source = text.trim();
    const enclosed = display.exec(source) ?? inline.exec(source);
    const body = enclosed?.[0] === source ? (enclosed[1] ?? enclosed[2]) : source;
    const html = formula(body, true);
    return { html: html ? passive(html) : `<pre>${escaped(source)}</pre>`, invalid: html === null };
  }

  let formulas = 0;
  const math = (level: "block" | "inline", expression: RegExp, startPattern: RegExp): TokenizerAndRendererExtension => {
    const name = `terminalMath${level}`;
    return {
      name, level,
      start(source) { const index = source.search(startPattern); if (index >= 0) return index; },
      tokenizer(source) {
        const match = expression.exec(source);
        if (match) return { type: name, raw: match[0], text: match[1] ?? match[2] };
      },
      renderer(token) {
        const html = ++formulas <= 128 ? formula(token.text, level === "block") : null;
        return html ?? `<code>${escaped(token.raw)}</code>`;
      },
    };
  };
  const parser = new Marked({
    extensions: [math("block", display, /\$\$|\\\[/), math("inline", inline, /\$|\\\(/)],
    renderer: {
      html({ text }) { return escaped(text); },
      link({ tokens }) { return this.parser.parseInline(tokens); },
      image({ text }) { return escaped(text); },
    },
  });
  try {
    return { html: passive(parser.parse(text, { async: false })), invalid: false };
  } catch {
    return { html: `<pre>${escaped(text)}</pre>`, invalid: true };
  }
}
