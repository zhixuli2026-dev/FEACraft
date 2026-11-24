import { ShapeUtils, Vector2 } from "three";

export type CadVec3 = number[];

export interface CadClipPlane {
  normal: CadVec3;
  point: CadVec3;
}

export interface CadClipResult {
  positions: CadVec3[];
  faces: number[][];
  uvs?: number[];
  capPositions: CadVec3[];
  capFaces: number[][];
  openContours: boolean;
}

function dot(a: CadVec3, b: CadVec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function sub(a: CadVec3, b: CadVec3): CadVec3 {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function cross(a: CadVec3, b: CadVec3): CadVec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function normalize(value: CadVec3): CadVec3 {
  const length = Math.hypot(...value);
  if (!Number.isFinite(length) || length <= 1e-12) throw new Error("剖切平面法线不能为零");
  return value.map(component => component / length);
}

function signedDistance(point: CadVec3, plane: CadClipPlane): number {
  return dot(plane.normal, sub(point, plane.point));
}

function clipPolygon(polygon: CadVec3[], plane: CadClipPlane) {
  const result: CadVec3[] = [];
  const intersections: CadVec3[] = [];
  for (let index = 0; index < polygon.length; index++) {
    const current = polygon[index];
    const next = polygon[(index + 1) % polygon.length];
    const currentDistance = signedDistance(current, plane);
    const nextDistance = signedDistance(next, plane);
    if ((currentDistance >= 0) !== (nextDistance >= 0)) {
      const ratio = currentDistance / (currentDistance - nextDistance);
      const intersection = current.map((value, axis) => value + ratio * (next[axis] - value));
      result.push(intersection);
      intersections.push(intersection);
    }
    if (currentDistance >= 0) result.push(current);
  }
  return {polygon: result, intersections};
}

function pointKey(point: CadVec3): string {
  return point.map(value => Math.round(value * 1e6).toString()).join(",");
}

function capLoops(edges: Array<[CadVec3, CadVec3]>): CadVec3[][] | null {
  const nodes = new Map<string, {point: CadVec3; links: Set<string>}>();
  const addNode = (key: string, point: CadVec3) => {
    if (!nodes.has(key)) nodes.set(key, {point, links: new Set()});
  };
  for (const [first, second] of edges) {
    const a = pointKey(first), b = pointKey(second);
    if (a === b) continue;
    addNode(a, first);
    addNode(b, second);
    nodes.get(a)!.links.add(b);
    nodes.get(b)!.links.add(a);
  }
  // An open exterior can enclose otherwise closed hole loops. Treat this
  // entire section as open to avoid incorrectly filling those holes.
  if (Array.from(nodes.values()).some(node => node.links.size !== 2)) return null;
  const visited = new Set<string>();
  const loops: CadVec3[][] = [];
  for (const start of nodes.keys()) {
    if (visited.has(start)) continue;
    const loop: CadVec3[] = [];
    let current = start;
    let previous: string | undefined;
    do {
      visited.add(current);
      const node = nodes.get(current)!;
      loop.push(node.point);
      const [first, second] = node.links;
      [previous, current] = [current, first === previous ? second : first];
    } while (current !== start);
    loops.push(loop);
  }
  return loops;
}

function loopNormal(loop: CadVec3[]): CadVec3 {
  const normal: CadVec3 = [0, 0, 0];
  for (let index = 0; index < loop.length; index++) {
    const current = loop[index], next = loop[(index + 1) % loop.length];
    normal[0] += (current[1] - next[1]) * (current[2] + next[2]);
    normal[1] += (current[2] - next[2]) * (current[0] + next[0]);
    normal[2] += (current[0] - next[0]) * (current[1] + next[1]);
  }
  return normal;
}

function orientLoop(loop: CadVec3[], normal: CadVec3): CadVec3[] {
  return dot(loopNormal(loop), normal) < 0 ? [...loop].reverse() : loop;
}

function area2D(contour: Vector2[]): number {
  let area = 0;
  for (let index = 0; index < contour.length; index++) {
    const current = contour[index], next = contour[(index + 1) % contour.length];
    area += current.x * next.y - next.x * current.y;
  }
  return Math.abs(area / 2);
}

function containsPoint(point: Vector2, contour: Vector2[]): boolean {
  let inside = false;
  for (let index = 0, previous = contour.length - 1; index < contour.length; previous = index++) {
    const a = contour[index], b = contour[previous];
    if ((a.y > point.y) !== (b.y > point.y) &&
        point.x < (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

function appendCaps(loops: CadVec3[][], planeNormal: CadVec3, positions: CadVec3[], faces: number[][]) {
  const reference = Math.abs(planeNormal[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
  const u = normalize(cross(planeNormal, reference));
  const v = normalize(cross(planeNormal, u));
  const projected = loops.filter(loop => loop.length >= 3).map(loop => {
    const contour = loop.map(point => new Vector2(dot(point, u), dot(point, v)));
    return {loop, contour, area: area2D(contour), depth: 0, holes: [] as CadVec3[][]};
  }).sort((a, b) => a.area - b.area);
  for (let index = projected.length - 1; index >= 0; index--) {
    const item = projected[index];
    // A vertex of a disjoint contour is inside its enclosing contour even
    // when an average of the concave contour's vertices falls outside it.
    for (let enclosing = index + 1; enclosing < projected.length; enclosing++) {
      const parent = projected[enclosing];
      if (parent.area <= item.area || !containsPoint(item.contour[0], parent.contour)) continue;
      item.depth = parent.depth + 1;
      if (item.depth % 2 === 1) parent.holes.push(item.loop);
      break;
    }
  }
  for (const outer of projected.filter(item => item.depth % 2 === 0)) {
    const ordered = [orientLoop(outer.loop, planeNormal),
      ...outer.holes.map(hole => orientLoop(hole, planeNormal.map(component => -component)))];
    const contours = ordered.map(loop => loop.map(point => new Vector2(dot(point, u), dot(point, v))));
    const triangles = ShapeUtils.triangulateShape(contours[0], contours.slice(1));
    const flat = ordered.flat();
    const base = positions.length;
    positions.push(...flat);
    for (const [a, b, c] of triangles) {
      const faceNormal = cross(sub(flat[b], flat[a]), sub(flat[c], flat[a]));
      faces.push(dot(faceNormal, planeNormal) >= 0 ? [base + a, base + b, base + c] : [base + a, base + c, base + b]);
    }
  }
}

export function clipCadMesh(vertices: readonly CadVec3[], faces: readonly (readonly number[])[],
  plane: CadClipPlane, uvs?: readonly number[]): CadClipResult {
  const normalizedPlane = {normal: normalize(plane.normal), point: [...plane.point]};
  const coordinates = uvs && vertices.map((point, i) => [...point, uvs[i * 2], uvs[i * 2 + 1]]);
  const positions: CadVec3[] = [];
  const retainedUVs = coordinates ? [] as number[] : undefined;
  const retainedFaces: number[][] = [];
  const capEdges: Array<[CadVec3, CadVec3]> = [];
  for (const face of faces) {
    if (face.length !== 3 || face.some(index => !Number.isInteger(index) || index < 0 || index >= vertices.length)) {
      throw new Error("剖切输入必须包含有效三角面索引");
    }
    const {polygon, intersections} = clipPolygon(face.map(index => (coordinates ?? vertices)[index]), normalizedPlane);
    for (let index = 1; index < polygon.length - 1; index++) {
      const base = positions.length;
      const first = polygon[0], second = polygon[index], third = polygon[index + 1];
      if (retainedUVs) {
        for (const point of [first, second, third]) {
          positions.push(point.slice(0, 3));
          retainedUVs.push(point[3], point[4]);
        }
      } else positions.push(first, second, third);
      retainedFaces.push([base, base + 1, base + 2]);
    }
    if (intersections.length === 2) {
      const [first, second] = intersections;
      capEdges.push(coordinates ? [first.slice(0, 3), second.slice(0, 3)] : [first, second]);
    }
  }
  const capPositions: CadVec3[] = [];
  const capFaces: number[][] = [];
  const loops = capLoops(capEdges);
  if (loops) appendCaps(loops, normalizedPlane.normal.map(component => -component), capPositions, capFaces);
  return {positions, faces: retainedFaces, capPositions, capFaces, openContours: loops === null,
    ...(retainedUVs ? {uvs: retainedUVs} : {})};
}
