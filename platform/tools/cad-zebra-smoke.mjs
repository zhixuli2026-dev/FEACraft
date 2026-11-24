import assert from "node:assert/strict";
import {
  buildZebraPixels,
  normalizeZebraScheme,
  normalizeZebraMapping,
  normalizeZebraAngle,
  zebraDirectionVector,
  ZEBRA_TEXTURE_HEIGHT,
} from "../web/src/utils/cadZebra.ts";
import { clipCadMesh } from "../web/src/utils/cadClip.ts";
import { normalizeCadStudioMapping } from "../web/src/utils/cadStudioTexture.ts";

const blackwhite = buildZebraPixels(3, "blackwhite");
assert.equal(blackwhite.length, ZEBRA_TEXTURE_HEIGHT * 4);
assert.deepEqual([...blackwhite.slice(0, 4)], [0, 0, 0, 255]);
assert.deepEqual([...blackwhite.slice(-4)], [0, 0, 0, 255]);
const grayscale = buildZebraPixels(4, "grayscale");
assert.deepEqual([...grayscale.slice(0, 3)], [210, 210, 210]);
assert.deepEqual([...grayscale.slice(-4, -1)], [70, 70, 70]);
const colorful = buildZebraPixels(3, "colorful");
assert.deepEqual([...colorful.slice(0, 3)], [...colorful.slice(-4, -1)]);
console.log("1. blackwhite/grayscale/colorful 纹理与奇数条纹边界 [OK]");

assert.equal(normalizeZebraScheme("blackwhite"), "blackwhite");
assert.equal(normalizeZebraScheme("colorful"), "colorful");
assert.equal(normalizeZebraScheme("grayscale"), "grayscale");
assert.equal(normalizeZebraMapping("reflection"), "reflection");
assert.equal(normalizeZebraMapping("normal"), "normal");
assert.equal(normalizeZebraAngle(90), 90);
assert.deepEqual(zebraDirectionVector(0), [1, 0, 0]);
assert.ok(Math.abs(zebraDirectionVector(90)[0]) < 1e-12);
assert.ok(Math.abs(zebraDirectionVector(90)[1] - 1) < 1e-12);
console.log("2. scheme/mapping/angle 与方向向量 [OK]");
for (const invalid of ["blackred", "uv", "", null]) {
  assert.throws(() => normalizeZebraScheme(invalid));
  assert.throws(() => normalizeZebraMapping(invalid));
}
assert.throws(() => buildZebraPixels(1, "blackwhite"));
assert.throws(() => buildZebraPixels(51, "blackwhite"));
assert.throws(() => normalizeZebraAngle(-1));
assert.throws(() => normalizeZebraAngle(361));
console.log("3. 非法配色、mapping、条纹数和角度 fail closed [OK]");

const custom = buildZebraPixels(3, "custom", ["#fa2407", "#0be384"]);
const pixel = index => [...custom.slice(index * 4, (index + 1) * 4)];
assert.deepEqual(pixel(0), [250, 36, 7, 255]);
assert.deepEqual(pixel(1023), pixel(0));
assert.deepEqual(pixel(1024), [11, 227, 132, 255]);
assert.deepEqual(pixel(3071), pixel(1024));
assert.deepEqual(pixel(3072), pixel(0));
assert.deepEqual(pixel(4095), pixel(0));
assert.throws(() => buildZebraPixels(3, "custom", ["#fa2407", "red"]));
assert.equal(normalizeCadStudioMapping("triplanar"), "triplanar");
assert.equal(normalizeCadStudioMapping("parametric"), "parametric");
assert.throws(() => normalizeCadStudioMapping("unknown"));
console.log("4. 自定义条纹颜色与首尾半宽 [OK]");

const clipped = clipCadMesh([[-1, 0, 0], [1, 0, 0], [1, 2, 0]], [[0, 1, 2]],
  {normal: [1, 0, 0], point: [0, 0, 0]}, [0, 0, 1, 0, 1, 1]);
assert.equal(clipped.uvs.length, clipped.positions.length * 2);
assert.ok(clipped.positions.some(([x, y], i) => x === 0 && y === 0
  && clipped.uvs[i * 2] === 0.5 && clipped.uvs[i * 2 + 1] === 0));
assert.ok(clipped.positions.some(([x, y], i) => x === 0 && y === 1
  && clipped.uvs[i * 2] === 0.5 && clipped.uvs[i * 2 + 1] === 0.5));
console.log("5. 剖切交点保留插值 UV [OK]");

console.log("\n=== CAD B22 Zebra 配色与 mapping 纯函数冒烟通过 ===");
