import assert from "node:assert/strict";
import { resolveDocumentLink } from "../web/src/utils/documentLinks.ts";

const base = "E-report/build/report.html";
assert.deepEqual(resolveDocumentLink(base, "../../D-post/figure.png"), {kind: "case", path: "D-post/figure.png", hash: ""});
assert.deepEqual(resolveDocumentLink(base, "../report.docx#results"), {kind: "case", path: "E-report/report.docx", hash: "results"});
assert.deepEqual(resolveDocumentLink(base, "/0-caseDict/caseDict"), {kind: "case", path: "0-caseDict/caseDict", hash: ""});
assert.deepEqual(resolveDocumentLink(base, "figure%20one.png?x=1#plot"), {kind: "case", path: "E-report/build/figure one.png", hash: "plot"});
assert.deepEqual(resolveDocumentLink(base, "#results"), {kind: "internal", hash: "results"});
assert.deepEqual(resolveDocumentLink(base, "https://example.com/a"), {kind: "external", url: "https://example.com/a"});
for (const href of ["../../../../outside.txt", "javascript:alert(1)", "//evil.example/x", "data:text/html,x", ""]) {
  assert.deepEqual(resolveDocumentLink(base, href), {kind: "invalid"});
}
console.log("Document link resolution and unsafe-scheme rejection: OK");
