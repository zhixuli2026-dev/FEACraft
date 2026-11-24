import { Quaternion, Vector3 } from "three";

export interface CadSceneObject {
  name: string;
  color: string;
  alpha: number;
  vertices: number[][];
  faces: number[][];
  bbox: { min: number[]; max: number[] };
  normals?: number[][];
  uvs?: number[];
  edges?: number[][];
  treeId?: string;
  groups?: CadSceneGroup[];
}

export interface CadSceneGroup { id: string; name: string }

export interface CadSceneDocument {
  objects: CadSceneObject[];
}

export type CadTheme = "dark" | "light";

type RecordValue = Record<string, unknown>;

const COLOR = /^#[0-9a-fA-F]{6}$/;
const MAX_OBJECTS = 64;
const MAX_VERTICES = 100000;
const MAX_TRIANGLES = 200000;

function asRecord(value: unknown, label: string): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 必须是对象`);
  }
  return value as RecordValue;
}

function finite(value: unknown, label: string): number {
  if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) {
    throw new Error(`${label} 必须是有限数值`);
  }
  const result = Number(value);
  if (!Number.isFinite(result)) throw new Error(`${label} 必须是有限数值`);
  return result;
}

function vectors(value: unknown, label: string, limit: number): number[][] {
  if (!Array.isArray(value)) throw new Error(`${label} 必须是列表`);
  if (!value.length) return [];
  if (!Array.isArray(value[0])) {
    if (value.length % 3 !== 0) throw new Error(`${label} 扁平长度必须是 3 的倍数`);
    if (value.length / 3 > limit) throw new Error(`${label} 不能超过 ${limit} 个三维向量`);
    const result: number[][] = [];
    for (let index = 0; index < value.length; index += 3) {
      result.push([
        finite(value[index], label),
        finite(value[index + 1], label),
        finite(value[index + 2], label),
      ]);
    }
    return result;
  }
  if (value.length > limit) throw new Error(`${label} 不能超过 ${limit} 个三维向量`);
  return value.map((item) => {
    if (!Array.isArray(item) || item.length !== 3) throw new Error(`${label} 每项必须是三维向量`);
    return item.map((component) => finite(component, label));
  });
}

function vector3(value: unknown, label: string): number[] {
  const result = vectors(value, label, 1);
  if (result.length !== 1) throw new Error(`${label} 必须是三维向量`);
  return result[0];
}

function textureCoordinates(value: unknown, vertexCount: number): number[] | undefined {
  if (!Array.isArray(value)) throw new Error("UV 坐标必须是列表");
  if (!value.length) return undefined;
  if (value.length > vertexCount * 2) throw new Error("UV 数量与顶点不一致");
  const components = Array.isArray(value[0]) ? value.flatMap((pair: unknown) => {
    if (!Array.isArray(pair) || pair.length !== 2) throw new Error("每项 UV 必须包含两个坐标");
    return pair;
  }) : value;
  if (components.length !== vertexCount * 2) throw new Error("UV 数量与顶点不一致");
  return components.map(component => {
    const coordinate = finite(component, "UV");
    if (!Number.isFinite(Math.fround(coordinate))) throw new Error("UV 坐标超出范围");
    return coordinate;
  });
}

function triangles(value: unknown, vertexCount: number): number[][] {
  if (!Array.isArray(value)) throw new Error("faces/triangles 必须是列表");
  if (!value.length) return [];
  const polygons: number[][] = [];
  const tooMany = () => new Error(`三角面不能超过 ${MAX_TRIANGLES} 个`);
  if (!Array.isArray(value[0])) {
    if (value.length % 3 !== 0) throw new Error("扁平三角索引长度必须是 3 的倍数");
    if (value.length / 3 > MAX_TRIANGLES) throw tooMany();
    for (let index = 0; index < value.length; index += 3) {
      polygons.push([value[index], value[index + 1], value[index + 2]].map((item) => finite(item, "面索引")));
    }
  } else {
    if (value.length > MAX_TRIANGLES) throw tooMany();
    for (const face of value) {
      if (!Array.isArray(face) || face.length < 3) throw new Error("每个面至少包含三个索引");
      if (polygons.length + face.length - 2 > MAX_TRIANGLES) throw tooMany();
      const polygon = face.map((item) => finite(item, "面索引"));
      for (let index = 1; index < polygon.length - 1; index++) {
        polygons.push([polygon[0], polygon[index], polygon[index + 1]]);
      }
    }
  }
  for (const polygon of polygons) {
    for (const index of polygon) {
      if (!Number.isInteger(index) || index < 0 || index >= vertexCount) {
        throw new Error("面索引必须为有效整数且不能越界");
      }
    }
  }
  return polygons;
}

function bounds(vertices: number[][]) {
  if (!vertices.length) throw new Error("空对象缺 bbox");
  const min = [...vertices[0]];
  const max = [...vertices[0]];
  for (const point of vertices) {
    for (const axis of [0, 1, 2]) {
      min[axis] = Math.min(min[axis], point[axis]);
      max[axis] = Math.max(max[axis], point[axis]);
    }
  }
  return {min, max};
}

function sceneObject(value: unknown): CadSceneObject {
  const item = asRecord(value, "场景对象");
  const name = item.name;
  if (typeof name !== "string" || name.length < 1 || name.length > 120) {
    throw new Error("对象名称须为 1–120 字符");
  }
  const color = item.color ?? "#2563eb";
  if (typeof color !== "string" || !COLOR.test(color)) throw new Error("对象颜色须为 #RRGGBB");
  const alpha = finite(item.alpha ?? 1, "alpha");
  if (alpha < 0 || alpha > 1) throw new Error("alpha 须在 [0,1]");
  const vertices = vectors(item.vertices, "vertices", MAX_VERTICES);
  const faces = triangles(item.faces ?? item.triangles, vertices.length);
  let bbox: { min: number[]; max: number[] };
  const rawBb = item.bb ? asRecord(item.bb, "bb") : null;
  const rawBBox = item.bbox ?? (rawBb ? {
    min: [rawBb.xmin, rawBb.ymin, rawBb.zmin],
    max: [rawBb.xmax, rawBb.ymax, rawBb.zmax],
  } : null);
  if (rawBBox) {
    const record = asRecord(rawBBox, "bbox");
    bbox = {min: vector3(record.min, "bbox.min"), max: vector3(record.max, "bbox.max")};
    for (let axis = 0; axis < 3; axis++) {
      if (bbox.min[axis] > bbox.max[axis]) throw new Error("bbox.min 不能大于 bbox.max");
    }
  } else {
    bbox = bounds(vertices);
  }
  const uvs = item.uvs == null ? undefined : textureCoordinates(item.uvs, vertices.length);
  return {name, color, alpha, vertices, faces, bbox, ...(uvs ? {uvs} : {})};
}

export function normalizeCadScene(value: unknown): CadSceneDocument {
  const document = asRecord(value, "场景");
  if (!Array.isArray(document.objects) || document.objects.length > MAX_OBJECTS) {
    throw new Error(`objects 必须是至多 ${MAX_OBJECTS} 项的列表`);
  }
  const used = new Set<string>();
  return {objects: document.objects.map((value) => {
    const object = sceneObject(value);
    return {...object, name: nextName(object.name, used)};
  })};
}

function nextName(original: string, names: Set<string>) {
  let name = original;
  for (let suffix = 2; names.has(name); suffix++) {
    const tail = ` (${suffix})`;
    name = original.slice(0, 120 - tail.length) + tail;
  }
  names.add(name);
  return name;
}

function fromBase64(encoded: string) {
  const binary = atob(encoded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function unpack(value: unknown, label: string, dtype: "float32" | "int32", maxItems: number): unknown {
  if (Array.isArray(value)) return value;
  const packed = asRecord(value, label);
  const shape = packed.shape;
  if (packed.codec !== "b64" || packed.dtype !== dtype || typeof packed.buffer !== "string"
      || !Array.isArray(shape) || shape.length !== 1 || !Number.isInteger(shape[0])
      || shape[0] < 0 || shape[0] > maxItems || packed.buffer.length !== Math.ceil(shape[0] * 4 / 3) * 4) {
    throw new Error(`${label} 数据格式或长度无效`);
  }
  const bytes = fromBase64(packed.buffer);
  if (bytes.length !== shape[0] * 4) throw new Error(`${label} 缓冲区长度无效`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return Array.from({length: shape[0]}, (_, i) =>
    dtype === "float32" ? view.getFloat32(i * 4, true) : view.getInt32(i * 4, true));
}

function unpackOcpScene(value: unknown): CadSceneDocument {
  const message = asRecord(value, "OCP 消息");
  const data = asRecord(message.data, "OCP 数据");
  const instances = data.instances ?? [];
  if (!Array.isArray(instances) || instances.length > MAX_OBJECTS) throw new Error("OCP 实例数无效");

  const objects: CadSceneObject[] = [];
  const names = new Set<string>();
  const treeIds = new Set<string>();
  let nodeCount = 0;
  const walk = (raw: unknown, shift: Vector3, rotation: Quaternion, depth: number,
                groups: CadSceneGroup[], position: string) => {
    if (++nodeCount > MAX_OBJECTS * 4 || depth > 32) throw new Error("OCP 场景层级超限");
    const node = asRecord(raw, "OCP 部件");
    if (node.loc != null) {
      if (!Array.isArray(node.loc) || node.loc.length !== 2 || !Array.isArray(node.loc[1]) || node.loc[1].length !== 4) {
        throw new Error("OCP 部件变换无效");
      }
      const position = new Vector3().fromArray(vector3(node.loc[0], "OCP 位移"));
      const quaternion = new Quaternion().fromArray(node.loc[1].map((v: unknown) => finite(v, "OCP 旋转")));
      if (Math.abs(quaternion.lengthSq() - 1) > 1e-4) throw new Error("OCP 旋转须为单位四元数");
      shift = shift.clone().add(position.applyQuaternion(rotation));
      rotation = rotation.clone().multiply(quaternion.normalize());
    }
    if (Array.isArray(node.parts)) {
      const single = node.parts.length === 1 ? asRecord(node.parts[0], "OCP 部件") : undefined;
      const label = typeof node.name === "string" ? node.name : "";
      const branch = depth > 0 && label && !(single?.name === label && !Array.isArray(single.parts))
        ? [...groups, {id: cadTreeId(node.id, position, treeIds), name: label}] : groups;
      node.parts.forEach((child, index) => walk(child, shift, rotation, depth + 1, branch, `${position}/${index}`));
      return;
    }
    if (node.type !== "shapes" || objects.length >= MAX_OBJECTS) throw new Error("OCP 网格部件类型或数量无效");
    let shape = asRecord(node.shape, "OCP 网格");
    if (shape.ref !== undefined) {
      const reference = shape.ref;
      if (typeof reference !== "number" || !Number.isInteger(reference) || reference < 0 || reference >= instances.length) {
        throw new Error("OCP 网格实例索引无效");
      }
      shape = asRecord(instances[reference], "OCP 网格实例");
    }

    const mesh = sceneObject({
      ...node, bb: undefined, bbox: undefined,
      vertices: unpack(shape.vertices, "OCP 顶点", "float32", MAX_VERTICES * 3),
      faces: unpack(shape.triangles, "OCP 三角面", "int32", MAX_TRIANGLES * 3),
      uvs: shape.uvs == null ? undefined : unpack(shape.uvs, "OCP UV", "float32", MAX_VERTICES * 2),
    });
    if (!mesh.faces.length) throw new Error("OCP 部件没有可显示的三角面");
    const name = nextName(mesh.name, names);

    const point = new Vector3();
    const transform = (xyz: number[], offset: Vector3) => {
      const result = point.fromArray(xyz).applyQuaternion(rotation).add(offset).toArray();
      if (result.some((coordinate) => !Number.isFinite(Math.fround(coordinate)))) throw new Error("OCP 坐标超出范围");
      return result;
    };
    const vertices = mesh.vertices.map((vertex) => transform(vertex, shift));
    const object = {...mesh, name, vertices, bbox: bounds(vertices), groups, treeId: cadTreeId(node.id, position, treeIds)};
    const normals = shape.normals == null ? undefined : vectors(
      unpack(shape.normals, "OCP 法线", "float32", MAX_VERTICES * 3), "OCP 法线", MAX_VERTICES,
    );
    if (normals?.length) {
      if (normals.length !== vertices.length) throw new Error("OCP 法线数量与顶点不一致");
      const zero = new Vector3();
      object.normals = normals.map((normal) => transform(normal, zero));
    }
    if (shape.edges != null) {
      const edgePoints = vectors(unpack(shape.edges, "OCP 边线", "float32", MAX_TRIANGLES * 6), "OCP 边线", MAX_TRIANGLES * 2);
      if (edgePoints.length % 2 !== 0) throw new Error("OCP 边线缺少配对端点");
      object.edges = edgePoints.map((endpoint) => transform(endpoint, shift));
    }
    objects.push(object);
  };
  walk(data.shapes, new Vector3(), new Quaternion(), 0, [], "");
  if (!objects.length) throw new Error("OCP 场景没有可显示的网格");
  return {objects};
}

function cadTreeId(value: unknown, position: string, used: Set<string>) {
  let id = typeof value === "string" && value.startsWith("/") && value.length <= 768
    ? `path:${value}` : `index:${position}`;
  if (used.has(id)) id = `index:${position}`;
  used.add(id);
  return id;
}

export function cadAssemblyPayload(value: unknown): unknown {
  const scene = asRecord(value, "场景");
  const packet = scene.ocp ?? scene.ocpMessage;
  if (!packet || typeof packet !== "object" || Array.isArray(packet)) return undefined;
  type Shape = {ref?: number; uvs?: unknown};
  const data = (packet as {data?: {shapes?: {parts?: unknown}; instances?: Shape[]}}).data;
  const parts = data?.shapes?.parts;
  if (!Array.isArray(parts)) return undefined;
  const hasNativeView = parts.some((child: {parts?: unknown; shape?: Shape} | null) => {
    if (Array.isArray(child?.parts)) return true;
    const shape = child?.shape;
    const coordinates = (typeof shape?.ref === "number" ? data?.instances?.[shape.ref] : shape)?.uvs;
    if (Array.isArray(coordinates)) return coordinates.length > 0;
    const size = (coordinates as {shape?: unknown} | undefined)?.shape;
    return Array.isArray(size) && typeof size[0] === "number" && size[0] > 0;
  });
  return hasNativeView ? packet : undefined;
}

export function normalizeCadAssembly(payload: unknown) {
  return unpackOcpScene(payload);
}

export function decodeCadSceneBase64(encoded: string): CadSceneDocument {
  if (typeof encoded !== "string" || !encoded.trim()) throw new Error("Base64 场景不能为空");
  try {
    const text = new TextDecoder("utf-8", {fatal: true}).decode(fromBase64(encoded));
    const scene = asRecord(JSON.parse(text), "场景");
    const ocp = scene.ocp ?? scene.ocpMessage ?? (scene.type === "data" ? scene : undefined);
    if (ocp != null) {
      try {
        return unpackOcpScene(ocp);
      } catch (error) {
        if (!Array.isArray(scene.objects) || !scene.objects.length) throw error;
      }
    }
    return normalizeCadScene(scene);
  } catch (error) {
    throw new Error(`Base64 场景无效：${error instanceof Error ? error.message : String(error)}`);
  }
}

export function cadFitDistance(size: readonly number[], fovDegrees: number, aspect: number, margin = 1.2): number {
  if (size.length !== 3) throw new Error("Fit size 必须是三维向量");
  const dimensions = size.map((value) => Math.abs(finite(value, "Fit size")));
  const fov = finite(fovDegrees, "fov");
  const widthAspect = finite(aspect, "aspect");
  const safeMargin = finite(margin, "margin");
  if (fov <= 0 || fov >= 180 || widthAspect <= 0 || safeMargin <= 0) {
    throw new Error("fov/aspect/margin 参数无效");
  }
  const radius = Math.max(Math.hypot(...dimensions) / 2, 1e-6);
  const vertical = fov * Math.PI / 180;
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * widthAspect);
  const limiting = Math.min(vertical, horizontal);
  return radius / Math.sin(limiting / 2) * safeMargin;
}

export function cadOpacityFor(baseAlpha: number, transparent: boolean): number {
  const alpha = finite(baseAlpha, "alpha");
  if (alpha < 0 || alpha > 1) throw new Error("alpha 须在 [0,1]");
  return transparent ? Math.min(alpha, 0.5) : alpha;
}

export function normalizeCadTheme(value: unknown): CadTheme {
  return value === "dark" ? "dark" : "light";
}
