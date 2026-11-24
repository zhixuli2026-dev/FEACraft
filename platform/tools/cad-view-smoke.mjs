import assert from "node:assert/strict";
import {
  cadOrthoSpan,
  normalizeCadControl,
  normalizeCadViewMode,
  normalizeCadMaterial,
  cadQuadViewports,
} from "../web/src/utils/cadView.ts";

const diameter = Math.hypot(2, 2, 2);
assert.ok(Math.abs(cadOrthoSpan([2, 2, 2], 1) - diameter * 1.2) < 1e-12);
assert.ok(Math.abs(cadOrthoSpan([2, 2, 2], 0.5) - diameter * 1.2 * 2) < 1e-12);
assert.ok(Math.abs(cadOrthoSpan([2, 2, 2], 2) - diameter * 1.2) < 1e-12);
assert.ok(cadOrthoSpan([0, 0, 0], 1) > 0);
assert.throws(() => cadOrthoSpan([2, 2], 1));
assert.throws(() => cadOrthoSpan([2, 2, 2], 0));
console.log("1. 正交取景 span、aspect 与非法参数 [OK]");

assert.equal(normalizeCadControl("trackball"), "trackball");
assert.equal(normalizeCadControl("orbit"), "orbit");
assert.equal(normalizeCadControl("other"), "orbit");
assert.equal(normalizeCadViewMode("quad"), "quad");
assert.equal(normalizeCadViewMode("single"), "single");
assert.equal(normalizeCadViewMode("other"), "single");
console.log("2. orbit/trackball 与 single/quad 归一化 [OK]");

assert.deepEqual(normalizeCadMaterial({}), {
  ambientIntensity: 1,
  directIntensity: 1.1,
  metalness: 0.3,
  roughness: 0.65,
});
assert.deepEqual(normalizeCadMaterial({ambientIntensity: 0.5, directIntensity: 1.5, metalness: 0.8, roughness: 0.2}), {
  ambientIntensity: 0.5,
  directIntensity: 1.5,
  metalness: 0.8,
  roughness: 0.2,
});
for (const invalid of [
  {ambientIntensity: -1}, {directIntensity: 5}, {metalness: 1.1}, {roughness: -0.1}, {roughness: Number.NaN},
]) assert.throws(() => normalizeCadMaterial(invalid));
console.log("3. 材质默认值、显式值与范围拒绝 [OK]");

const viewports = cadQuadViewports(801, 601);
assert.deepEqual(viewports.map(v => v.preset), ["iso", "x", "y", "z"]);
assert.equal(viewports.reduce((sum, v) => sum + v.width * v.height, 0), 801 * 601);
for (const viewport of viewports) {
  assert.ok(viewport.width > 0 && viewport.height > 0);
  assert.ok(viewport.x >= 0 && viewport.y >= 0);
  assert.ok(viewport.x + viewport.width <= 801);
  assert.ok(viewport.y + viewport.height <= 601);
}
assert.throws(() => cadQuadViewports(0, 600));
assert.throws(() => cadQuadViewports(800, -1));
console.log("4. 奇数尺寸四宫格无重叠/缺口的视口计算 [OK]");

console.log("\n=== CAD B09 视图与材质纯函数冒烟通过 ===");
