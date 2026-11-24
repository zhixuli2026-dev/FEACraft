import assert from "node:assert/strict";
import { langOf } from "../web/src/utils/fileLanguage.ts";
import { FOAM_LANG } from "../web/src/utils/openfoamLang.ts";

for (const path of [
  "C-run/case-1/0/U",
  "C-run/case-1/0/p",
  "C-run/case-1/0.1/U",
  "C-run/case-1/0.orig/U",
  "C-run/case-1/0/alpha.water",
  "C-run/case-1/system/controlDict",
  "C-run/case-1/constant/transportProperties",
  "C-run/case-1/case-1.foam",
]) {
  assert.equal(langOf(path), FOAM_LANG, `${path} 应按 OpenFOAM 着色`);
}
assert.equal(langOf("README"), "plaintext");
assert.equal(langOf("Ref/notes"), "plaintext");
assert.equal(langOf("A-geo/geometry.py"), "python");
assert.equal(langOf("scripts/run.sh"), "shell");
assert.equal(langOf("Ref/.gitignore"), "plaintext");
assert.equal(langOf("C-run/case-1/0/check.py"), "python");
assert.equal(langOf("C-run/case-1/0/README.md"), "markdown");
assert.equal(langOf("C-run/case-1/0/notes.txt"), "plaintext");
assert.equal(langOf("C-run/case-1/100/U", FOAM_LANG), FOAM_LANG);
assert.equal(langOf("C-run/case-1/1e-3/fluid/alpha.water", FOAM_LANG), FOAM_LANG);
assert.equal(langOf("Ref/2026/notes"), "plaintext");
console.log("OpenFOAM/普通无扩展名与常规扩展名语言判定 [OK]");
