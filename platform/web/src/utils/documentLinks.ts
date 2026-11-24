export type ResolvedDocumentLink =
  | { kind: "case"; path: string; hash: string }
  | { kind: "internal"; hash: string }
  | { kind: "external"; url: string }
  | { kind: "invalid" };

/** 将阅读器链接解析为算例资源、页内锚点或明确的 http(s) 外链。 */
export function resolveDocumentLink(baseFile: string, href: string): ResolvedDocumentLink {
  const reference = href.trim();
  if (!reference) return { kind: "invalid" };
  if (reference.startsWith("#")) {
    return reference.length > 1 ? { kind: "internal", hash: reference.slice(1) } : { kind: "invalid" };
  }
  if (/^https?:\/\//i.test(reference)) return { kind: "external", url: reference };
  if (reference.startsWith("//") || /^[A-Za-z][A-Za-z\d+.-]*:/.test(reference)) return { kind: "invalid" };
  const hashIndex = reference.indexOf("#");
  const hash = hashIndex >= 0 ? reference.slice(hashIndex + 1) : "";
  const withoutHash = hashIndex >= 0 ? reference.slice(0, hashIndex) : reference;
  const rawPath = withoutHash.split("?", 1)[0];
  if (!rawPath) return hash ? { kind: "internal", hash } : { kind: "invalid" };
  let decoded: string;
  try {
    decoded = decodeURIComponent(rawPath);
  } catch {
    return { kind: "invalid" };
  }
  if (!decoded || decoded.includes("\x00") || decoded.includes("\\")) return { kind: "invalid" };
  const normalizedBase = baseFile.replace(/\\/g, "/");
  const parts = decoded.startsWith("/") ? [] : normalizedBase.split("/").slice(0, -1);
  for (const part of decoded.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return { kind: "invalid" };
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  return parts.length ? { kind: "case", path: parts.join("/"), hash } : { kind: "invalid" };
}
