import assert from "node:assert/strict";
import { formatCitation } from "../web/src/utils/citationStyle.ts";

const item = {title: "OpenFOAM verification", authors: ["Jane Doe", "John Smith"], year: "2026", id: "doi:10.1000/demo"};
assert.equal(formatCitation(item, "apa"), "Doe, J. & Smith, J. (2026). OpenFOAM verification. https://doi.org/10.1000/demo");
assert.equal(formatCitation(item, "ieee", 2), '[2] J. Doe and J. Smith, "OpenFOAM verification," 2026. doi: 10.1000/demo.');
assert.equal(formatCitation(item, "chicago"), 'Doe, Jane, and John Smith. 2026. "OpenFOAM verification." https://doi.org/10.1000/demo');
const many = {...item, authors: ["Doe, Jane", "A B", "C D", "E F"], year: ""};
assert.equal(formatCitation(many, "apa"), "Doe, J. et al. (n.d.). OpenFOAM verification. https://doi.org/10.1000/demo");
assert.equal(formatCitation({...item, id: "arXiv:2401.00001", authors: []}, "apa"), "(2026). OpenFOAM verification. https://arxiv.org/abs/2401.00001");
console.log("APA/IEEE/Chicago author/year/DOI/arXiv formatting: OK");
