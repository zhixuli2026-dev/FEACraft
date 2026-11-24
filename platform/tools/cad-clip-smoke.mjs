import assert from "node:assert/strict";
import { clipCadMesh } from "../web/src/utils/cadClip.ts";

const vertices = [
  [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
  [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
];
const faces = [
  [0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7],
  [0, 4, 5], [0, 5, 1], [3, 2, 6], [3, 6, 7],
  [0, 3, 7], [0, 7, 4], [1, 5, 6], [1, 6, 2],
];
const sub = (a, b) => a.map((v, i) => v - b[i]);
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
const area = (positions, faces) => faces.reduce((sum, face) => {
  const value = cross(sub(positions[face[1]], positions[face[0]]), sub(positions[face[2]], positions[face[0]]));
  return sum + Math.hypot(...value) / 2;
}, 0);

const clipped = clipCadMesh(vertices, faces, {normal: [0, 0, -1], point: [0, 0, 0]});
assert.equal(clipped.faces.length, 14);
assert.equal(clipped.capFaces.length, 6);
assert.ok(Math.abs(area(clipped.capPositions, clipped.capFaces) - 4) < 1e-9);
assert.ok(clipped.positions.every(point => point[2] <= 1e-9));
assert.ok(clipped.capPositions.every(point => Math.abs(point[2]) < 1e-9));
for (const face of clipped.capFaces) {
  const normal = cross(sub(clipped.capPositions[face[1]], clipped.capPositions[face[0]]),
    sub(clipped.capPositions[face[2]], clipped.capPositions[face[0]]));
  assert.ok(dot(normal, [0, 0, 1]) > 0);
}
console.log("1. 立方体中截面保留面、闭环端盖面积与法线 [OK]");
const flipped = clipCadMesh(vertices, faces, {normal: [0, 0, 1], point: [0, 0, 0]});
assert.ok(flipped.positions.every(point => point[2] >= -1e-9));
assert.ok(Math.abs(area(flipped.capPositions, flipped.capFaces) - 4) < 1e-9);
for (const face of flipped.capFaces) {
  const [a, b, c] = face.map(index => flipped.capPositions[index]);
  assert.ok(dot(cross(sub(b, a), sub(c, a)), [0, 0, -1]) > 0);
}

const all = clipCadMesh(vertices, faces, {normal: [0, 0, -1], point: [0, 0, 2]});
assert.equal(all.faces.length, 12);
assert.equal(all.capFaces.length, 0);
const none = clipCadMesh(vertices, faces, {normal: [0, 0, -1], point: [0, 0, -2]});
assert.equal(none.faces.length, 0);
assert.equal(none.capFaces.length, 0);
console.log("2. 平面外全保留/全丢弃不产生伪端盖 [OK]");

const tetra = clipCadMesh(
  [[-1, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
  [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]],
  {normal: [-1, 0, 0], point: [0, 0, 0]},
);
assert.equal(tetra.capFaces.length, 1);
assert.ok(Math.abs(area(tetra.capPositions, tetra.capFaces) - 0.5) < 1e-9);
console.log("3. 四面体三角端盖面积与闭环焊接 [OK]");

const ringVertices = [];
for (const z of [-1, 1]) {
  for (const [x, y] of [[-2, -2], [2, -2], [2, 2], [-2, 2], [-1, -1], [1, -1], [1, 1], [-1, 1]]) {
    ringVertices.push([x, y, z]);
  }
}
const ringFaces = [];
const addQuad = (a, b, c, d) => ringFaces.push([a, b, c], [a, c, d]);
for (let i = 0; i < 4; i++) {
  const j = (i + 1) % 4;
  addQuad(i, j, 4 + j, 4 + i);
  addQuad(8 + i, 8 + j, 12 + j, 12 + i);
  addQuad(i, j, 8 + j, 8 + i);
  addQuad(4 + i, 4 + j, 12 + j, 12 + i);
}
const ring = clipCadMesh(ringVertices, ringFaces, {normal: [0, 0, -1], point: [0, 0, 0]});
assert.ok(Math.abs(area(ring.capPositions, ring.capFaces) - 12) < 1e-9,
  `带孔端盖面积应为 16-4=12，实际 ${area(ring.capPositions, ring.capFaces)}`);
console.log("4. 带孔端盖保留内孔，不把孔填成实体 [OK]");
const openRing = clipCadMesh(ringVertices, ringFaces.filter((_, index) => index !== 4 && index !== 5),
  {normal: [0, 0, -1], point: [0, 0, 0]});
assert.ok(openRing.openContours && openRing.faces.length > 0 && openRing.capFaces.length === 0);
console.log("5. 外轮廓断开时保留剖切表面和内孔，不构造端盖 [OK]");

assert.throws(() => clipCadMesh(vertices, faces, {normal: [0, 0, 0], point: [0, 0, 0]}));
assert.throws(() => clipCadMesh(vertices, [[0, 1, 99]], {normal: [0, 0, -1], point: [0, 0, 0]}));
console.log("6. 零法线与越界面索引 fail closed [OK]");

console.log("\n=== CAD B11 真实剖切与端盖几何冒烟通过 ===");
