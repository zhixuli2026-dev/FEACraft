import assert from "node:assert/strict";
import {
  decodeCadSceneBase64,
  normalizeCadScene,
  cadFitDistance,
  cadOpacityFor,
  normalizeCadTheme,
} from "../web/src/utils/cadScene.ts";

const encode = (value) => Buffer.from(JSON.stringify(value), "utf8").toString("base64");
const flatScene = {
  schema: "vibeflow.cadquery.scene.v1",
  objects: [{
    name: "立方体", color: "#00ff00", alpha: 0.8,
    vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
    triangles: [0, 1, 2, 0, 1, 3, 0, 2, 3, 1, 2, 3],
  }],
};
const decoded = decodeCadSceneBase64(encode(flatScene));
assert.equal(decoded.objects[0].name, "立方体");
assert.equal(decoded.objects[0].vertices.length, 4);
assert.equal(decoded.objects[0].faces.length, 4);
assert.deepEqual(decoded.objects[0].bbox, {min: [0, 0, 0], max: [1, 1, 1]});
assert.equal(decoded.objects[0].alpha, 0.8);
console.log("1. Base64 UTF-8、扁平 vertices/triangles 与 bbox 推导 [OK]");

const nested = normalizeCadScene({
  objects: [{
    name: "native", color: "#2563eb", alpha: 1,
    vertices: [[0, 0, 0], [2, 0, 0], [0, 2, 0]],
    faces: [[0, 1, 2]], bbox: {min: [0, 0, 0], max: [2, 2, 0]},
  }],
});
assert.deepEqual(nested.objects[0].faces, [[0, 1, 2]]);
assert.deepEqual(normalizeCadScene({objects: []}).objects, []);
console.log("2. 原生嵌套 scene.json 与空场景 [OK]");

const expected = (Math.hypot(2, 2, 2) / 2) / Math.sin((45 * Math.PI / 180) / 2) * 1.2;
assert.ok(Math.abs(cadFitDistance([2, 2, 2], 45, 1) - expected) < 1e-12);
assert.ok(cadFitDistance([2, 2, 2], 45, 0.5) > cadFitDistance([2, 2, 2], 45, 2));
assert.ok(Number.isFinite(cadFitDistance([0, 0, 0], 45, 1)) && cadFitDistance([0, 0, 0], 45, 1) > 0);
assert.equal(cadOpacityFor(0.8, true), 0.5);
assert.equal(cadOpacityFor(0.3, true), 0.3);
assert.equal(cadOpacityFor(0.8, false), 0.8);
assert.equal(normalizeCadTheme("dark"), "dark");
assert.equal(normalizeCadTheme("light"), "light");
assert.equal(normalizeCadTheme("sepia"), "light");
console.log("3. Fit 距离、透明 opacity 与主题归一化 [OK]");

for (const invalid of ["%%%", "e30=", Buffer.from([0xff, 0xfe]).toString("base64")]) {
  assert.throws(() => decodeCadSceneBase64(invalid));
}
assert.throws(() => normalizeCadScene({objects: [{name: "bad", vertices: [[0, 0, 0]], faces: [[0, 1, 2]]}]}));
assert.throws(() => normalizeCadScene({objects: [{name: "bad", color: "red", vertices: [[0, 0, 0]], faces: []}]}));
assert.throws(() => normalizeCadScene({objects: [{name: "bad", vertices: [[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces: [[0, 1, 99]]}]}));
assert.throws(() => normalizeCadScene({objects: [{name: "bad", vertices: [[0, 0, Number.NaN]], faces: []}]}));
assert.throws(() => cadFitDistance([2, 2], 45, 1));
assert.throws(() => cadFitDistance([2, 2, 2], 0, 1));
console.log("4. 非法 Base64/UTF-8/对象/面索引/数值与 Fit 参数 fail closed [OK]");

console.log("\n=== CAD B07 场景协议纯函数冒烟通过 ===");
