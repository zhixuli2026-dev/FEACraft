import assert from "node:assert/strict";
import { resolveLatexSource } from "../web/src/utils/latexDiagnostics.ts";

const main = "E-report/latex/main.tex";
assert.equal(resolveLatexSource(null, main), main);
assert.equal(resolveLatexSource("./E-report/latex/main.tex", main), main);
assert.equal(resolveLatexSource("E-report/latex/sections/results.tex", main), "E-report/latex/sections/results.tex");
assert.equal(resolveLatexSource("E-report\\shared\\macros.tex", main), "E-report/shared/macros.tex");
assert.equal(resolveLatexSource("..\\shared\\macros.tex", main), null);
assert.equal(resolveLatexSource("../../../../outside.tex", main), null);
assert.equal(resolveLatexSource("/mnt/d/outside.tex", main), null);
assert.equal(resolveLatexSource("C:/outside.tex", main), null);
assert.equal(resolveLatexSource("image.png", main), null);
console.log("LaTeX diagnostic source resolution: OK");
