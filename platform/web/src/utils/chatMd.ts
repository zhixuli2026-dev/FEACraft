import DOMPurify from "dompurify";
import { renderMarkdownMath } from "./markdownMath.ts";

export function renderChatMd(text: string): string {
  // Tokenize Markdown to preserve literal math inside code, then sanitize the rendered HTML.
  return DOMPurify.sanitize(renderMarkdownMath(text), {
    ADD_TAGS: ["math", "semantics", "mrow", "mi", "mo", "mn", "msup", "msub",
               "mfrac", "msqrt", "mstyle", "mtext", "mspace", "annotation",
               "msubsup", "munderover", "mtable", "mtr", "mtd", "munder",
               "mover", "menclose", "mphantom", "mpadded", "mspace"],
    ADD_ATTR: ["aria-hidden", "encoding", "style"],
  });
}
