import assert from "node:assert/strict";
import { TerminalOutputQueue, OUTPUT_CHUNK_BYTES, rewriteLightSgr, TerminalSgrStream } from "../web/src/utils/terminalOutput.ts";

const queue = new TerminalOutputQueue();
const encode = (text) => new TextEncoder().encode(text);
const decode = (data) => new TextDecoder().decode(data);
queue.push(encode("A".repeat(123) + "B".repeat(2 * 1024 * 1024)));
assert.equal(queue.queuedBytes, 2 * 1024 * 1024);
const first = queue.nextChunk();
assert.equal(first.bytes.length, OUTPUT_CHUNK_BYTES);
assert.equal(first.truncated, true);
assert.equal(decode(first.bytes), "B".repeat(OUTPUT_CHUNK_BYTES));
let chunks = 1;
while (true) {
  const chunk = queue.nextChunk();
  if (chunk === null) break;
  assert.equal(chunk.truncated, false);
  chunks++;
}
assert.equal(chunks, 32);
assert.equal(queue.queuedBytes, 0);
queue.push(encode("BEGIN"));
queue.push(encode("END"));
assert.equal(decode(queue.nextChunk().bytes), "BEGINEND");
assert.equal(queue.nextChunk(), null);
queue.push(encode("discard"));
queue.clear();
assert.equal(queue.nextChunk(), null);
queue.push(encode("x".repeat(OUTPUT_CHUNK_BYTES - 1) + "🙂中"));
const streaming = new TextDecoder();
assert.equal(streaming.decode(queue.nextChunk().bytes, {stream:true}), "x".repeat(OUTPUT_CHUNK_BYTES - 1));
assert.equal(streaming.decode(queue.nextChunk().bytes), "🙂中");
queue.clear();
queue.push(encode("A".repeat((2 * 1024 * 1024) - 1)));
queue.nextChunk();
queue.push(encode("中文内容🙂"));
let tail = "";
for (let chunk; (chunk = queue.nextChunk());) tail += streaming.decode(chunk.bytes, {stream:true});
assert.equal(tail.slice(-6), "中文内容🙂");
assert.equal(rewriteLightSgr("\x1b[30;40;1mX\x1b[0m"),
  "\x1b[30;48;2;241;245;249;1mX\x1b[0m");
assert.equal(rewriteLightSgr("\x1b[100mX"), "\x1b[48;2;226;232;240mX");
assert.equal(rewriteLightSgr("\x1b[31;42mX"), "\x1b[31;42mX");
assert.equal(rewriteLightSgr("\x1b[38;2;40;50;100;48;5;100;40mX"),
  "\x1b[38;2;40;50;100;48;5;100;48;2;241;245;249mX");
assert.equal(rewriteLightSgr("\x1b[38:2::40:50:100mX"), "\x1b[38:2::40:50:100mX");
const sgr = new TerminalSgrStream();
assert.equal(sgr.decode("old\x1b[3", true), "old");
assert.equal(sgr.decode("8;2;40;", true), "");
assert.equal(sgr.decode("50;100;40mX\x1b", true), "\x1b[38;2;40;50;100;48;2;241;245;249mX");
assert.equal(sgr.decode("[100mY", true), "\x1b[48;2;226;232;240mY");
assert.equal(sgr.decode("\x1b[", false), "");
sgr.clear();
assert.equal(sgr.decode("40mZ", true), "40mZ");
console.log("Terminal pending/queue 2MiB, 64KiB chunks, light SGR: OK");
