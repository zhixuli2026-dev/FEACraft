import { mathjax } from "mathjax-full/js/mathjax.js";
import { liteAdaptor } from "mathjax-full/js/adaptors/liteAdaptor.js";
import { RegisterHTMLHandler } from "mathjax-full/js/handlers/html.js";
import { AssistiveMmlHandler } from "mathjax-full/js/a11y/assistive-mml.js";
import { SafeHandler } from "mathjax-full/js/ui/safe/SafeHandler.js";
import { MathML } from "mathjax-full/js/input/mathml.js";
import { TeX } from "mathjax-full/js/input/tex.js";
import { SVG } from "mathjax-full/js/output/svg.js";
import "mathjax-full/js/input/tex/ams/AmsConfiguration.js";
import "mathjax-full/js/input/tex/cases/CasesConfiguration.js";
import "mathjax-full/js/input/tex/empheq/EmpheqConfiguration.js";
import "mathjax-full/js/input/tex/newcommand/NewcommandConfiguration.js";
import "mathjax-full/js/input/tex/noerrors/NoErrorsConfiguration.js";
import "mathjax-full/js/input/tex/noundefined/NoUndefinedConfiguration.js";
import "mathjax-full/js/input/tex/textmacros/TextMacrosConfiguration.js";

const adaptor = liteAdaptor();
AssistiveMmlHandler(SafeHandler(RegisterHTMLHandler(adaptor)));

export function renderCompatibleMarkdown(html: string) {
  const document = mathjax.document(`<div class="vf-preview-ignore">${html}</div>`, {
    InputJax: [new TeX({
      packages: ["base", "ams", "cases", "empheq", "newcommand", "noerrors", "noundefined", "textmacros"],
      inlineMath: [["\\(", "\\)"]], displayMath: [["\\[", "\\]"]], processEscapes: true, processEnvironments: true,
    }), new MathML()],
    OutputJax: new SVG({ fontCache: "none" }),
    ignoreHtmlClass: "vf-preview-ignore",
    processHtmlClass: "vf-preview-tex",
    skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    safeOptions: { allow: { URLs: "none", classes: "none", cssIDs: "none", styles: "none" } },
  });
  document.render();
  return adaptor.innerHTML(adaptor.body(document.document));
}
