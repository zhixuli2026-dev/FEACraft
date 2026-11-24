export type PathFormat = "windows" | "linux";

export function formatPath(path: string, format: PathFormat): string {
  const posix = path.replace(/\\/g, "/");
  return format === "windows" ? posix.replace(/\//g, "\\") : posix;
}

export interface PathReference {
  path: string;
  startLine?: number;
  endLine?: number;
  startColumn?: number;
  endColumn?: number;
  error?: string;
}

const REFERENCE_RE = /(?:^|\s)@([\w\-./]+)(:\S*)?(?=\s|$)/g;
const RANGE_RE = /^:(\d+)(?:-(\d+))?(?::(\d+)-(\d+))?$/;

export function parsePathReferences(text: string): PathReference[] {
  const references: PathReference[] = [];
  for (const match of text.matchAll(REFERENCE_RE)) {
    const [, path, suffix] = match;
    if (!suffix) {
      references.push({ path });
      continue;
    }
    const range = suffix.match(RANGE_RE);
    if (!range) {
      references.push({ path, error: "行列范围格式无效" });
      continue;
    }
    const startLine = Number(range[1]);
    const endLine = range[2] ? Number(range[2]) : startLine;
    const startColumn = range[3] ? Number(range[3]) : undefined;
    const endColumn = range[4] ? Number(range[4]) : undefined;
    const ref: PathReference = {
      path, startLine, endLine, startColumn, endColumn,
    };
    if (startLine < 1 || endLine < startLine) ref.error = "行范围无效";
    if (startColumn !== undefined && endColumn !== undefined &&
        (startColumn < 1 || endColumn < startColumn)) ref.error = "列范围无效";
    references.push(ref);
  }
  return references;
}

export function sliceTextByReference(content: string, ref: PathReference): string {
  if (ref.error) throw new Error(ref.error);
  if (ref.startLine === undefined) return content.slice(0, 4000);
  const endLine = ref.endLine ?? ref.startLine;
  const lines = content.split(/\r?\n/);
  if (ref.startLine > lines.length || endLine > lines.length) {
    throw new Error(`引用行号越界（文件共 ${lines.length} 行）`);
  }
  let selected = lines.slice(ref.startLine - 1, endLine);
  const { startColumn, endColumn } = ref;
  if (startColumn !== undefined && endColumn !== undefined) {
    const shortLine = selected.findIndex((line) => endColumn > line.length);
    if (shortLine !== -1) {
      throw new Error(`引用列号越界（第 ${ref.startLine + shortLine} 行共 ${selected[shortLine].length} 列）`);
    }
    selected = selected.map((line) => line.slice(startColumn - 1, endColumn));
  }
  return selected.join("\n").slice(0, 4000);
}

export function formatAttachment(content: string, ref: PathReference): string {
  let label = ref.path;
  if (ref.startLine !== undefined) {
    label += `:${ref.startLine}`;
    if (ref.endLine !== ref.startLine) label += `-${ref.endLine}`;
    if (ref.startColumn !== undefined && ref.endColumn !== undefined) {
      label += `:${ref.startColumn}-${ref.endColumn}`;
    }
  }
  return `\n\n[附件 ${label}]\n\`\`\`\n${sliceTextByReference(content, ref)}\n\`\`\``;
}
