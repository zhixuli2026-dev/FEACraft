import { useMemo, useState } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";
import CitationPanel from "./CitationPanel";
import { insertAtCursor } from "../shell/editorRef";
import { activatePanel } from "../shell/dock";
import { useStore } from "../store";

/** 公式调色板（自有实现；语义对齐原版 LatexFormulaEditorDialog：
 *  分类网格点选 → 源码编辑 → KaTeX 即时预览 → 包裹格式 → 插入编辑器光标；OCR 入口跳公式识别页） */

// 分类符号集（标准 LaTeX 命令，事实性数据）
const PALETTE: Array<[string, Array<[string, string]>]> = [
  ["希腊小写", [["α", "\\alpha"], ["β", "\\beta"], ["γ", "\\gamma"], ["δ", "\\delta"],
    ["ε", "\\varepsilon"], ["θ", "\\theta"], ["λ", "\\lambda"], ["μ", "\\mu"],
    ["ν", "\\nu"], ["ξ", "\\xi"], ["π", "\\pi"], ["ρ", "\\rho"], ["σ", "\\sigma"],
    ["τ", "\\tau"], ["φ", "\\varphi"], ["χ", "\\chi"], ["ψ", "\\psi"], ["ω", "\\omega"]]],
  ["希腊大写", [["Γ", "\\Gamma"], ["Δ", "\\Delta"], ["Θ", "\\Theta"], ["Λ", "\\Lambda"],
    ["Π", "\\Pi"], ["Σ", "\\Sigma"], ["Φ", "\\Phi"], ["Ψ", "\\Psi"], ["Ω", "\\Omega"]]],
  ["运算/关系", [["±", "\\pm"], ["×", "\\times"], ["÷", "\\div"], ["·", "\\cdot"],
    ["≤", "\\leq"], ["≥", "\\geq"], ["≠", "\\neq"], ["≈", "\\approx"], ["∝", "\\propto"],
    ["∞", "\\infty"], ["∂", "\\partial"], ["∇", "\\nabla"], ["∈", "\\in"], ["⊂", "\\subset"],
    ["∪", "\\cup"], ["∩", "\\cap"], ["∀", "\\forall"], ["∃", "\\exists"], ["→", "\\rightarrow"], ["⇒", "\\Rightarrow"]]],
  ["结构", [["a/b", "\\frac{a}{b}"], ["√x", "\\sqrt{x}"], ["ⁿ√x", "\\sqrt[n]{x}"],
    ["x²", "x^{2}"], ["xᵢ", "x_{i}"], ["xᵢ²", "x_{i}^{2}"], ["Σ", "\\sum_{i=1}^{n}"],
    ["∫", "\\int_{a}^{b}"], ["∮", "\\oint"], ["lim", "\\lim_{x \\to 0}"],
    ["∂/∂t", "\\frac{\\partial u}{\\partial t}"], ["∇·u", "\\nabla \\cdot \\mathbf{u}"]]],
  ["矩阵/括号", [["(·)", "\\left( \\cdot \\right)"], ["[·]", "\\left[ \\cdot \\right]"],
    ["|·|", "\\left| \\cdot \\right|"], ["‖·‖", "\\left\\| \\cdot \\right\\|"],
    ["{cases}", "\\begin{cases} a & x>0 \\\\ b & x\\leq 0 \\end{cases}"],
    ["bmatrix", "\\begin{bmatrix} a & b \\\\ c & d \\end{bmatrix}"],
    ["aligned", "\\begin{aligned} a &= b \\\\ &= c \\end{aligned}"]]],
  ["CFD 常用", [["∇²p", "\\nabla^{2} p"], ["ρ", "\\rho"], ["ν", "\\nu"],
    ["Re", "\\mathrm{Re}"], ["Ca", "\\mathrm{Ca}"], ["Δp", "\\Delta p"],
    ["u·n", "\\mathbf{u} \\cdot \\mathbf{n}"], ["‖U‖", "\\|\\mathbf{U}\\|"]]],
];

type WrapMode = "inline" | "display" | "equation";

export default function FormulaPanel() {
  const scope = useStore(state => JSON.stringify([state.user?.id, state.currentCase?.id]));
  const [src, setSrc] = useState("");
  const [wrap, setWrap] = useState<WrapMode>("inline");
  const [label, setLabel] = useState("");
  const [cat, setCat] = useState(0);
  const [insertion, setInsertion] = useState<{ role: "status" | "alert"; message: string } | null>(null);

  const preview = useMemo(() => {
    if (!src.trim()) return null;
    try {
      return { html: katex.renderToString(src, { displayMode: wrap !== "inline", throwOnError: true, trust: false }) };
    } catch (error) {
      return { error: error instanceof Error ? error.message : String(error) };
    }
  }, [src, wrap]);

  const insert = () => {
    if (!src.trim()) return;
    const { currentCase, currentWs, activeFile, openFiles } = useStore.getState();
    if (currentWs?.access_role === "viewer") {
      setInsertion({ role: "alert", message: "当前工作区为只读，公式源码已保留。" });
      return;
    }
    if (!currentCase || !activeFile || openFiles[activeFile]?.kind !== "text") {
      setInsertion({ role: "alert", message: "请先打开要插入公式的文本文件。" });
      return;
    }
    let body = src;
    if (wrap === "inline") body = `$${src}$`;
    else if (wrap === "display") body = `$$\n${src}\n$$`;
    else {
      const key = label.trim();
      if (/[{}\\%\r\n]/.test(key)) {
        setInsertion({ role: "alert", message: "标签不可包含花括号、反斜杠、百分号或换行。" });
        return;
      }
      const lbl = key ? `\n\\label{${key}}` : "";
      body = `\\begin{equation}\n${src}${lbl}\n\\end{equation}`;
    }
    activatePanel("editor");
    if (!insertAtCursor(body, activeFile)) {
      setInsertion({ role: "alert", message: "请在编辑器的源码视图中定位光标后重试。" });
      return;
    }
    setSrc("");
    setInsertion({ role: "status", message: `已插入 ${activeFile.split("/").pop()}，请保存文件。` });
  };

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 10, fontSize: 13,
                  color: "#d7dce5", display: "flex", flexDirection: "column", gap: 8 }}>
      {/* 分类页签 + 符号网格 */}
      <div role="group" aria-label="符号分类" style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {PALETTE.map(([name], i) => (
          <button type="button" key={name} aria-pressed={cat === i}
                  style={{ ...s.chip, background: cat === i ? "#1d3557" : "#141a24",
                                      color: cat === i ? "#7db4ff" : "#8b93a7" }}
                  onClick={() => setCat(i)}>{name}</button>
        ))}
      </div>
      <div role="group" aria-label="当前分类符号" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(56px, 1fr))",
                    gap: 4 }}>
        {PALETTE[cat][1].map(([label, tex]) => (
          <button type="button" key={label} style={s.sym} title={tex} onClick={() => {
            setSrc((x) => x + (x && !x.endsWith(" ") ? " " : "") + tex);
            setInsertion(null);
          }}>
            {label}
          </button>
        ))}
      </div>

      {/* 源码 + 预览 */}
      <textarea aria-label="LaTeX 公式源码" style={s.src} placeholder="LaTeX 公式源码（点上面符号快速输入）"
                value={src} onChange={(e) => { setSrc(e.target.value); setInsertion(null); }} />
      {preview?.html && <div data-testid="formula-preview" style={s.preview} dangerouslySetInnerHTML={{ __html: preview.html }} />}
      {preview?.error && <div data-testid="formula-preview-error" role="alert" className="vf-project-error">{preview.error}</div>}

      {/* 包裹格式 + 标签 + 插入 */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        <select aria-label="公式包裹格式" style={s.select} value={wrap}
                onChange={(e) => { setWrap(e.target.value as WrapMode); setInsertion(null); }}>
          <option value="inline">行内 $…$</option>
          <option value="display">展示 $$…$$</option>
          <option value="equation">equation 环境</option>
        </select>
        {wrap === "equation" && (
          <input aria-label="公式标签" style={s.label} placeholder="标签（可选，如 eq:energy）"
                 value={label} onChange={(e) => { setLabel(e.target.value); setInsertion(null); }} />
        )}
        <button type="button" style={s.btn} onClick={insert} disabled={!src.trim()}>插入编辑器光标处</button>
        <button style={s.miniBtn} onClick={() => activatePanel("texo")}
                title="从截图识别公式（公式识别标签页）">OCR 识别</button>
      </div>
      {insertion && <div role={insertion.role} data-testid="formula-insertion" className="vf-project-error">{insertion.message}</div>}

      <CitationPanel key={scope} />
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  chip: { padding: "2px 8px", border: "1px solid #22304a", borderRadius: 5,
          cursor: "pointer", fontSize: 11 },
  sym: { padding: "6px 2px", background: "#141a24", color: "#cfd6e4",
         border: "1px solid #22304a", borderRadius: 6, cursor: "pointer",
         fontSize: 14, textAlign: "center" },
  src: { minHeight: 64, padding: 8, background: "#0f1115", color: "#e8eaf0",
         border: "1px solid #2a2f3a", borderRadius: 8, fontSize: 13,
         fontFamily: "Consolas, monospace", resize: "vertical" },
  preview: { background: "#fff", color: "#111", borderRadius: 8, padding: "12px 14px",
             overflow: "auto", fontSize: 16 },
  select: { padding: "4px 6px", background: "#0f1115", color: "#e8eaf0",
            border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  label: { flex: 1, minWidth: 100, padding: "4px 8px", background: "#0f1115",
           color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  btn: { padding: "5px 14px", background: "#2563eb", color: "#fff", border: "none",
         borderRadius: 6, cursor: "pointer", fontSize: 12 },
  miniBtn: { padding: "5px 10px", background: "#1a1e27", color: "#9ab",
             border: "1px solid #2a2f3a", borderRadius: 6, cursor: "pointer", fontSize: 12 },
};
