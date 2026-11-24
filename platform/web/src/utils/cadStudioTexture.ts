import { Material, RepeatWrapping, SRGBColorSpace, Texture, Vector3 } from "three";
import type { BufferGeometry, MeshStandardMaterial } from "three";

export type CadStudioMapping = "triplanar" | "parametric";

export function normalizeCadStudioMapping(value: unknown): CadStudioMapping {
  if (value === "triplanar" || value === "parametric") return value;
  throw new Error("未知的 Studio 纹理映射");
}

export async function decodeCadStudioTexture(file: File) {
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 8 * 1024 * 1024) {
    throw new Error("请选择 8 MiB 以内的 PNG、JPEG 或 WebP 图片。");
  }
  const image = await createImageBitmap(file, {imageOrientation: "flipY"}).catch(() => {
    throw new Error("无法读取这张图片。请重新选择纹理。");
  });
  if (image.width > 4096 || image.height > 4096) {
    image.close();
    throw new Error("纹理宽度和高度须在 4096 像素以内。");
  }
  const texture = new Texture(image);
  texture.flipY = false;
  texture.wrapS = texture.wrapT = RepeatWrapping;
  texture.colorSpace = SRGBColorSpace;
  texture.needsUpdate = true;
  return {texture, dispose() { texture.dispose(); image.close(); }};
}

const projectionVaryings = "varying vec3 cadTexturePosition; varying vec3 cadTextureNormal;\n";
const projectionSample = `
  vec3 weights = pow(abs(normalize(cadTextureNormal)), vec3(4.0));
  weights /= max(weights.x + weights.y + weights.z, 1e-6);
  vec3 p = (cadTexturePosition - cadTextureCenter) * cadTextureScale + 0.5;
  diffuseColor *= weights.x * texture2D(map, p.zy)
                + weights.y * texture2D(map, p.xz)
                + weights.z * texture2D(map, p.xy);
`;

export function setCadStudioTexture(material: MeshStandardMaterial, geometry: BufferGeometry,
                                   texture: Texture | null, mapping: CadStudioMapping,
                                   projectionGeometry = geometry) {
  const projected = !!texture && (mapping === "triplanar" || !geometry.hasAttribute("uv"));
  if (material.map === texture && !!material.userData.cadTextureProjected === projected) return;
  material.map = texture;
  material.userData.cadTextureProjected = projected;
  material.onBeforeCompile = Material.prototype.onBeforeCompile;
  if (projected) {
    if (!projectionGeometry.boundingBox) projectionGeometry.computeBoundingBox();
    const center = projectionGeometry.boundingBox?.getCenter(new Vector3()) ?? new Vector3();
    const size = projectionGeometry.boundingBox?.getSize(new Vector3()) ?? new Vector3();
    material.onBeforeCompile = shader => {
      shader.uniforms.cadTextureCenter = {value: center};
      shader.uniforms.cadTextureScale = {value: 1 / Math.max(size.x, size.y, size.z, 0.001)};
      shader.vertexShader = projectionVaryings + shader.vertexShader.replace("#include <begin_vertex>",
        "#include <begin_vertex>\ncadTexturePosition = position; cadTextureNormal = normal;");
      shader.fragmentShader = projectionVaryings
        + "uniform vec3 cadTextureCenter; uniform float cadTextureScale;\n"
        + shader.fragmentShader.replace("#include <map_fragment>", projectionSample);
    };
  }
  material.needsUpdate = true;
}
