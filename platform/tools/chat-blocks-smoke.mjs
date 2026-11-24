import assert from "node:assert/strict";
import { splitChatBlocks } from "../web/src/utils/chatBlocks.ts";

const source = "First paragraph.\n\n```python\nline one\n\nline two\n```\n\nLast paragraph.";
const blocks = splitChatBlocks(source);
assert.equal(blocks.length, 3, blocks);
assert.equal(blocks[0].text, "First paragraph.");
assert.ok(blocks[1].text.includes("line one\n\nline two"));
assert.equal(blocks[2].text, "Last paragraph.");
assert.deepEqual(blocks.map(block => block.key), ["block-0", "block-1", "block-2"]);
const appended = splitChatBlocks(source + "\n\nMore text");
assert.equal(appended.length, 4);
assert.equal(appended[0].text, blocks[0].text);
assert.equal(appended[1].text, blocks[1].text);
assert.equal(appended[2].text, blocks[2].text);
assert.equal(appended[3].text, "More text");
assert.equal(splitChatBlocks("").length, 0);
console.log("Chat markdown stable blocks/fences/append behavior: OK");
