import assert from "node:assert/strict";
import { parseDelimited } from "../web/src/utils/delimited.ts";

const csv = `name,note,value\nalpha,"comma,inside",1\nbeta,"quote ""inside""",2\ngamma,"first line\nsecond line",3\n space ," keep ",4\n`;
const parsed = parseDelimited(csv);
assert.deepEqual(parsed.errors, []);
assert.equal(parsed.rows.length, 5);
assert.equal(parsed.rows[1][1], "comma,inside");
assert.equal(parsed.rows[2][1], 'quote "inside"');
assert.equal(parsed.rows[3][1], "first line\nsecond line");
assert.equal(parsed.rows[4][0], " space ");
assert.equal(parsed.rows[4][1], " keep ");
assert.deepEqual(parseDelimited("a\tb\n1\t2").rows, [["a", "b"], ["1", "2"]]);
assert.deepEqual(parseDelimited("a;b\n1;2").rows, [["a", "b"], ["1", "2"]]);
assert.ok(parseDelimited('a,b\n"unterminated,2').errors.length > 0);
console.log("CSV quotes/escaped quotes/newlines/whitespace/delimiters/errors: OK");
