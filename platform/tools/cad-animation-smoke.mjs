import assert from "node:assert/strict";
import {
  normalizeCadAnimation,
  sampleCadAnimation,
  canonicalCadAnimationJson,
  cadSelectorMatches,
  bindCadAnimationTracks,
} from "../web/src/utils/cadAnimation.ts";

const document = {
  schema: "vibeflow.cadquery.animation.v1",
  speed: 1,
  repeat: false,
  tracks: [
    {selector: "/auto", action: "t", times: [0, 1], values: [[0, 0, 0], [1, 2, 3]]},
    {selector: "/auto", action: "tx", times: [0, 1], values: [0, 0.5]},
    {selector: "/auto", action: "ty", times: [0, 1], values: [0, 1]},
    {selector: "/auto", action: "tz", times: [0, 1], values: [0, 1.5]},
    {selector: "/auto", action: "q", times: [0, 1], values: [[0, 0, 0, 1], [0, 0, 0.7071067811865476, 0.7071067811865476]]},
    {selector: "/auto", action: "rx", times: [0, 1], values: [0, 30]},
    {selector: "/auto", action: "ry", times: [0, 1], values: [0, 60]},
    {selector: "/auto", action: "rz", times: [0, 1], values: [0, 90]},
  ],
};
const normalized = normalizeCadAnimation(document);
assert.equal(normalized.duration, 1);
assert.equal(normalized.tracks.length, 8);
const samples = sampleCadAnimation(normalized, 0.5);
assert.deepEqual(samples[0].value, [0.5, 1, 1.5]);
assert.equal(samples[1].value, 0.25);
assert.equal(samples[2].value, 0.5);
assert.equal(samples[3].value, 0.75);
assert.ok(Math.abs(samples[4].value[2] - Math.sin(Math.PI / 8)) < 1e-12);
assert.ok(Math.abs(samples[4].value[3] - Math.cos(Math.PI / 8)) < 1e-12);
assert.equal(samples[5].value, 15);
assert.equal(samples[6].value, 30);
assert.equal(samples[7].value, 45);
assert.equal(sampleCadAnimation(normalized, -1)[0].value[0], 0);
assert.equal(sampleCadAnimation(normalized, 2)[0].value[0], 1);
console.log("1. 八类轨道采样、角度/四元数插值与端点钳制 [OK]");

const roundtrip = JSON.parse(canonicalCadAnimationJson(normalized));
assert.equal(roundtrip.schema, document.schema);
assert.equal(roundtrip.tracks.length, 8);
assert.equal(cadSelectorMatches("/auto", "auto"), true);
assert.equal(cadSelectorMatches("auto", "/auto"), true);
assert.equal(cadSelectorMatches("/group/auto", "group/auto"), true);
assert.equal(cadSelectorMatches("/other", "auto"), false);
assert.equal(normalizeCadAnimation({...document, repeat: "false"}).repeat, false);
assert.equal(normalizeCadAnimation({...document, repeat: undefined, loop: false}).repeat, false);
const colliding = [{name: "drive/rotor", key: "path:/frame/drive/rotor"},
  {name: "drive_rotor", key: "path:/frame/holder/rotor"}];
const target = { ...document, tracks: [{...document.tracks[0], selector: "drive/rotor"}] };
assert.equal(bindCadAnimationTracks(normalizeCadAnimation(target), colliding)[0].key, colliding[0].key);
const ambiguous = {...target, tracks: [{...target.tracks[0], selector: "drive|rotor"}]};
assert.throws(() => bindCadAnimationTracks(normalizeCadAnimation(ambiguous), colliding), /匹配多个/);
const stable = {...target, tracks: [{...target.tracks[0], objectId: colliding[1].key}]};
assert.equal(bindCadAnimationTracks(normalizeCadAnimation(stable), colliding)[0].key, colliding[1].key);
assert.equal(JSON.parse(canonicalCadAnimationJson(stable)).tracks[0].objectId, colliding[1].key);
const identical = [{...colliding[0]}, {...colliding[1], name: colliding[0].name}];
assert.throws(() => bindCadAnimationTracks(normalizeCadAnimation(target), identical), /匹配多个/);
assert.equal(bindCadAnimationTracks(normalizeCadAnimation(stable), identical)[0].key, identical[1].key);
console.log("2. v1 规范化往返与选择器匹配 [OK]");

for (const invalid of [
  {...document, schema: "other"},
  {...document, speed: 0},
  {...document, speed: [1]},
  {...document, repeat: "no"},
  {...document, tracks: [{selector: "/auto", action: "tx", times: [0, 1], values: [" ", 1]}]},
  {...document, tracks: [{selector: "/auto", action: "bad", times: [0, 1], values: [0, 1]}]},
  {...document, tracks: [{selector: "/auto", action: "tx", times: [1, 0], values: [0, 1]}]},
  {...document, tracks: [{selector: "/auto", action: "tx", times: [0, 1], values: [0]}]},
  {...document, tracks: [{selector: "/auto", action: "q", times: [0, 1], values: [[0, 0, 0, 0], [0, 0, 0, 1]]}]},
  {...document, tracks: [{selector: "/auto", action: "t", times: [0, 1], values: [[0, 0], [1, 2, 3]]}]},
  {...document, tracks: []},
]) assert.throws(() => normalizeCadAnimation(invalid));
console.log("3. schema、时长、action、时间、values、四元数与空轨道 fail closed [OK]");

console.log("\n=== CAD B13 关键帧轨道纯函数冒烟通过 ===");
