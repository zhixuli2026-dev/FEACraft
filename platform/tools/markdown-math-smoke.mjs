import assert from "node:assert/strict";
import { renderMarkdownMath } from "../web/src/utils/markdownMath.ts";

const source = `# Math\n\nInline $E=mc^2$ and \\(a+b\\).\n\n$$\n\\nabla \\cdot \\mathbf{u}=0\n$$\n\n\\[\nx_i^2\n\\]\n\n\`$not_math$\`\n\n\`\`\`text\n$also_not_math$\n\`\`\`\n\nCurrency $5 and $6 remains text.`;
const html = renderMarkdownMath(source);
assert.ok((html.match(/class="katex"/g) ?? []).length >= 4, html);
assert.ok(html.includes("katex-display"), html);
assert.ok(html.includes("<code>$not_math$</code>"), html);
assert.ok(html.includes("$also_not_math$"), html);
assert.ok(html.includes("Currency $5 and $6 remains text."), html);
const mixed = renderMarkdownMath("Price $5. Formula $2x+1$. Range $5-$6.");
assert.ok(mixed.includes("Price $5. Formula "), mixed);
assert.ok(mixed.includes(">2x+1</annotation>"), mixed);
assert.ok(mixed.includes("Range $5-$6."), mixed);
const unsafe = renderMarkdownMath("$\\href{javascript:alert(1)}{click}$");
assert.ok(!unsafe.includes("<a ") && !unsafe.includes("href="), unsafe);
assert.ok(unsafe.includes("application/x-tex"), "原始 TeX 可作为非执行 annotation 保留");
console.log("Markdown KaTeX inline/display delimiters, code exclusion and trust boundary: OK");
