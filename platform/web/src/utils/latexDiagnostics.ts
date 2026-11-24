/** 注册编译接口返回算例相对的诊断源文件。 */
export function resolveLatexSource(file: string | null, main: string): string | null {
  const source = (file || main).replace(/\\/g, "/").replace(/^\.\//, "");
  if (!source.toLowerCase().endsWith(".tex") || /^(?:\/|[A-Za-z]:)/.test(source)) return null;
  if (source.split("/").some((part) => !part || part === "." || part === "..")) return null;
  return source;
}
