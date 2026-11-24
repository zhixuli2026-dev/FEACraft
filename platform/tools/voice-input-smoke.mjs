import assert from "node:assert/strict";
import { VoiceController } from "../web/src/utils/voiceInput.ts";

let constructed = 0;
let started = 0;
let stopped = 0;
let aborted = 0;
const states = [];
const transcripts = [];
class FakeRecognition {
  constructor() { constructed++; }
  start() { started++; }
  stop() { stopped++; }
  abort() { aborted++; }
}
const controller = new VoiceController(() => new FakeRecognition(),
  text => transcripts.push(text), state => states.push(state));
assert.equal(constructed, 0);
controller.start();
assert.equal(constructed, 1);
assert.equal(started, 1);
controller.recognition.onresult({resultIndex:0,results: [{isFinal:false,0:{transcript:"速度"}}]});
assert.deepEqual(transcripts, []);
controller.recognition.onresult({resultIndex:0,results: [{isFinal:true,0:{transcript:"速度 0.5 米每秒"}}]});
assert.deepEqual(transcripts, ["速度 0.5 米每秒"]);
controller.stop();
assert.equal(stopped, 1);
assert.deepEqual(states, ["listening", "processing"]);
const expiredError = controller.recognition.onerror;
const expiredEnd = controller.recognition.onend;
controller.recognition.onend();
assert.equal(states.at(-1), "stopped");
controller.start();
expiredError({error:"aborted"});
expiredEnd();
assert.equal(states.at(-1), "listening");
controller.stop();
assert.equal(stopped, 2);
assert.equal(aborted, 0);
assert.equal(states.at(-1), "processing");
controller.recognition.onend();
const failing = new VoiceController(() => new FakeRecognition(), () => {}, state => states.push(state));
failing.start();
failing.recognition.onerror({error:"not-allowed"});
assert.equal(states.at(-1), "error:not-allowed");
console.log("Voice click-to-start/transcript/stop/error without real microphone: OK");
