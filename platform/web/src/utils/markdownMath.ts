import katex from "katex";
import { Marked } from "marked";

const escapeHtml = (text: string) => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

function markdownParser(render: (text: string, display: boolean, bare?: boolean) => string) {
  const block = (name: string, opening: string, pattern: RegExp) => ({
    name,
    level: "block" as const,
    start: (source: string) => { const index = source.indexOf(opening); return index < 0 ? undefined : index; },
    tokenizer(source: string) {
      const match = pattern.exec(source);
      return match ? { type: name, raw: match[0], text: match[1] } : undefined;
    },
    renderer(token: { text: string }) { return `<div class="math-block">${render(token.text, true)}</div>\n`; },
  });
  const inline = (name: string, opening: string, pattern: RegExp, display = false, bare = false) => ({
    name,
    level: "inline" as const,
    start: (source: string) => { const index = source.indexOf(opening); return index < 0 ? undefined : index; },
    tokenizer(source: string) {
      const match = pattern.exec(source);
      return match ? { type: name, raw: match[0], text: match[1] } : undefined;
    },
    renderer(token: { text: string }) { return render(token.text, display, bare); },
  });
  const parser = new Marked({ gfm: true });
  parser.use({ extensions: [
    block("vfDisplayDollar", "$$", /^\$\$([\s\S]+?)\$\$[\t ]*(?:\r?\n|$)/),
    block("vfDisplayBracket", "\\[", /^\\\[([\s\S]+?)\\\][\t ]*(?:\r?\n|$)/),
    {
      name: "vfBareEnvironment",
      level: "block" as const,
      start(source) { const index = source.indexOf("\\begin{"); return index < 0 ? undefined : index; },
      tokenizer(source) {
        const start = /^\\begin\{([A-Za-z][A-Za-z0-9]*\*?)\}/.exec(source);
        if (!start) return;
        const closing = `\\end{${start[1]}}`;
        const end = source.indexOf(closing, start[0].length);
        if (end < 0) return;
        const text = source.slice(0, end + closing.length);
        return { type: "vfBareEnvironment", raw: text, text };
      },
      renderer(token: { text: string }) { return `<div class="math-block">${render(token.text, true, true)}</div>\n`; },
    },
    // A closing dollar sign must touch the expression and cannot start a price.
    inline("vfInlineDollar", "$", /^\$(?![\s$])((?:\\[^\r\n]|[^\\$\r\n])+?)(?<!\s)\$(?![$\d])/),
    inline("vfInlineParen", "\\(", /^\\\(([^\n]+?)\\\)/),
    inline("vfInsideDisplayDollar", "$$", /^\$\$([\s\S]+?)\$\$/, true),
    inline("vfInsideDisplayBracket", "\\[", /^\\\[([\s\S]+?)\\\]/, true),
    inline("vfEquationReference", "\\", /^(\\(?:eq)?ref\{[^}\n]+\})/, false, true),
  ] });
  return parser;
}

const katexMarkdown = markdownParser((text, displayMode) => katex.renderToString(text.trim(), {
  displayMode, throwOnError: false, strict: "warn", trust: false,
}));

const compatibilityMarkdown = markdownParser((text, display, bare) => {
  const escaped = escapeHtml(text.trim());
  const expression = bare ? escaped : display ? `\\[${escaped}\\]` : `\\(${escaped}\\)`;
  return `<span class="vf-preview-tex">${expression}</span>`;
});

/** File Markdown is parsed with a configuration independent of chat. */
export function renderMarkdownMath(source: string): string {
  return katexMarkdown.parse(source) as string;
}

export function prepareMarkdownPreview(source: string) {
  const contextual = /\\(?:def|[a-z]*newcommand|renewcommand|label|eqref|ref)\b|^\s*\\begin\{|<math(?=[\s>])/im;
  if (!contextual.test(source)) {
    const html = renderMarkdownMath(source);
    if (!html.includes('class="katex-error"')) return { html, compatibility: false };
  }
  return { html: compatibilityMarkdown.parse(source) as string, compatibility: true };
}
