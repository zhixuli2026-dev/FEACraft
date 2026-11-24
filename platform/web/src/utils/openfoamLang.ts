// OpenFOAM 字典语言支持（自有实现：Monarch tokenizer + 补全 provider 骨架）
// 契约语义对齐桌面版（关键字/块/向量/维数/注释/#include），文本与索引内容全自有。
import type { languages } from "monaco-editor";

export const FOAM_LANG = "openfoam-dict";

/** Monarch tokenizer：OpenFOAM 字典语法高亮 */
export const foamMonarch: languages.IMonarchLanguage = {
  defaultToken: "",
  tokenizer: {
    root: [
      [/\/\/.*$/, "comment"],
      [/\/\*[\s\S]*?\*\//, "comment"],
      [/#\s*\w+/, "keyword"],                       // #include/#calc 等预处理
      [/"(?:[^"\\]|\\.)*"/, "string"],
      [/-?\d+(\.\d+)?([eE][+-]?\d+)?/, "number"],
      [/\[\s*-?\d[\s\d.-]*\]/, "type"],             // 维数 [0 1 -2 0 0 0 0]
      [/\b(true|false|yes|no|on|off)\b/, "keyword"],
      [/\b(internalField|boundaryField|dimensions|object)\b/, "type"],
      [/\b[A-Z][\w]*(?=\s)/, "type"],               // 大写开头词（类名/场名）
      [/\b[a-z][\w]*(?=\s*[\{;])/, "keyword"],      // 块名/键名
      [/[{}()\[\];]/, "delimiter"],
    ],
  },
};

/** 注册语言（幂等） */
export function registerFoamLang(monaco: typeof import("monaco-editor")) {
  if (monaco.languages.getLanguages().some((l) => l.id === FOAM_LANG)) return;
  monaco.languages.register({ id: FOAM_LANG });
  monaco.languages.setMonarchTokensProvider(FOAM_LANG, foamMonarch);
}

// ---- 内置核心补全集（索引服务就绪前的保底；服务上线后由 provider 优先查服务端）----
const CORE_KEYS = [
  "application", "startFrom", "startTime", "stopAt", "endTime", "deltaT",
  "writeControl", "writeInterval", "purgeWrite", "writeFormat", "writePrecision",
  "runTimeModifiable", "adjustTimeStep", "maxCo", "libs", "functions",
  "internalField", "boundaryField", "dimensions", "FoamFile", "version",
  "format", "class", "object", "location", "solver", "preconditioner",
  "smoother", "tolerance", "relTol", "nSubIterations", "maxIter",
  "numberOfSubdomains", "method", "simple", "scotch", "hierarchical",
  "blockMesh", "checkMesh", "icoFoam", "simpleFoam", "pimpleFoam",
  "interFoam", "blockMeshDict", "controlDict", "fvSchemes", "fvSolution",
  "transportProperties", "turbulenceProperties",
];

export interface FoamCompletionItem {
  label: string;
  kinds?: string[];
  occurrences?: number;
  confidence?: number;
  sourceExamples?: string[];
}

export interface FoamCompletionQuery {
  q: string;
  parent?: string;
  context?: string;
  kind?: "dictionaryValue";
}

const CORE_VALUES: Record<string, string[]> = {
  writeControl: ["timeStep", "runTime", "adjustableRunTime", "cpuTime", "clockTime"],
  type: ["fixedValue", "fixedGradient", "zeroGradient", "calculated"],
};
const registered = new WeakSet<object>();
const statementTokens = /\/\*[\s\S]*?(?:\*\/|$)|\/\/[^\r\n]*|^[ \t]*#[^\r\n]*|"(?:[^"\\]|\\[\s\S])*(?:"|$)|[{};()[\]]|[^\s{};()[\]"/]+|[^\s]/gm;

function valueSite(beforeWord: string): { parent?: string; suppressed?: boolean } {
  let key: string | undefined;
  let hasValue = false;
  for (const match of beforeWord.matchAll(statementTokens)) {
    const token = match[0];
    const last = match.index + token.length === beforeWord.length;
    if (token.startsWith("/*") || token.startsWith("//")) {
      if ((token.startsWith("/*") && !token.endsWith("*/")) || (last && token.startsWith("//"))) {
        return { suppressed: true };
      }
      continue;
    }
    if (token.trimStart().startsWith("#")) {
      if (last) return { suppressed: true };
      key = undefined;
      hasValue = false;
    } else if (token === ";" || token === "{" || token === "}") {
      key = undefined;
      hasValue = false;
    } else if (last && token.startsWith('"') && (token.length === 1 || !token.endsWith('"'))) {
      return { suppressed: true };
    } else if (key === undefined && !hasValue && /^[A-Za-z_]\w{0,63}$/.test(token)) {
      key = token;
    } else {
      hasValue = true;
    }
  }
  return { parent: hasValue ? undefined : key };
}

export function registerFoamCompletion(
  monaco: typeof import("monaco-editor"),
  currentDocument: () => { path: string | null; caseId?: number },
  fetchCompletions: (query: FoamCompletionQuery) => Promise<{ ready: boolean; items: FoamCompletionItem[] }>,
) {
  registerFoamLang(monaco);
  if (registered.has(monaco)) return;
  registered.add(monaco);
  monaco.languages.registerCompletionItemProvider(FOAM_LANG, {
    triggerCharacters: [],
    provideCompletionItems: async (model, position, _context, cancellation) => {
      const document = currentDocument();
      const version = model.getVersionId();
      const line = model.getLineContent(position.lineNumber);
      const left = line.slice(0, position.column - 1);
      const prefix = left.match(/[\w#.$:+/-]+$/)?.[0] ?? "";
      if (prefix.length > 64) return { suggestions: [] };
      const range = {
        startLineNumber: position.lineNumber, endLineNumber: position.lineNumber,
        startColumn: position.column - prefix.length, endColumn: position.column,
      };
      const end = { lineNumber: position.lineNumber, column: range.startColumn };
      const start = model.getPositionAt(Math.max(0, model.getOffsetAt(end) - 8192));
      const { parent, suppressed } = valueSite(model.getValueInRange({
        startLineNumber: start.lineNumber, startColumn: start.column,
        endLineNumber: end.lineNumber, endColumn: end.column,
      }));
      if (suppressed) return { suggestions: [] };
      const context = document.path?.split("/").at(-1);
      const query: FoamCompletionQuery = { q: prefix };
      if (parent) {
        query.parent = parent;
        query.kind = "dictionaryValue";
        if (context && context.length <= 64) query.context = context;
      }
      const stale = () => {
        const current = currentDocument();
        return cancellation.isCancellationRequested || model.isDisposed() || model.getVersionId() !== version
          || current.path !== document.path || current.caseId !== document.caseId;
      };
      let remote: FoamCompletionItem[] = [];
      let ready = false;
      if (prefix || parent) {
        try {
          let reply = await fetchCompletions(query);
          if (stale()) return { suggestions: [] };
          if (reply.ready && query.context && !reply.items.length) {
            reply = await fetchCompletions({ ...query, context: undefined });
          }
          remote = reply.items;
          ready = reply.ready;
        } catch { /* The shipped core index is available when the API is interrupted. */ }
      }
      if (stale()) return { suggestions: [] };
      const seen = new Set(remote.map((item) => item.label));
      const core = parent ? (ready ? [] : CORE_VALUES[parent] ?? []) : CORE_KEYS;
      const local = core.filter((k) => !seen.has(k) && k.toLowerCase().startsWith(prefix.toLowerCase())).map((k) => ({
        label: k, kind: monaco.languages.CompletionItemKind.Keyword,
        insertText: k, range, detail: "内置补全", sortText: "1" + k,
      }));
      const remoteItems = remote.map((r, index) => ({
        label: r.label, kind: monaco.languages.CompletionItemKind.Keyword,
        insertText: r.label, range,
        detail: [r.kinds?.find((kind) => kind.endsWith(":source-backed")) ?? r.kinds?.[0],
          r.confidence === undefined ? undefined : `${Math.round(r.confidence * 100)}%`].filter(Boolean).join(" · "),
        documentation: r.sourceExamples?.join("\n"),
        sortText: "0" + String(index).padStart(3, "0"),
      }));
      return { suggestions: [...remoteItems, ...local] };
    },
  });
}
