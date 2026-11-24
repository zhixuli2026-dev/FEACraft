import { useEffect, useId, useMemo, useRef, useState } from "react";
import { renderTerminalSelection, terminalPreviewLimit } from "../utils/terminalSelectionPreview";

export default function TerminalSelectionPreview({ text, truncated, onClose }: {
  text: string;
  truncated: boolean;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const titleId = useId();
  const [mode, setMode] = useState<"markdown" | "latex">("markdown");
  const preview = useMemo(() => renderTerminalSelection(text, mode), [text, mode]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    titleRef.current?.focus();
  }, []);

  return <dialog ref={dialogRef} className="terminal-selection-preview" aria-labelledby={titleId} onClose={onClose}
    data-testid="terminal-selection-preview">
    <div className="terminal-preview-header">
      <h3 id={titleId} ref={titleRef} tabIndex={-1}>终端选区预览</h3>
      <button type="button" onClick={() => dialogRef.current?.close()}>关闭预览</button>
    </div>
    <div className="terminal-preview-format" role="group" aria-label="预览格式">
      <button type="button" aria-pressed={mode === "markdown"} onClick={() => setMode("markdown")}>Markdown</button>
      <button type="button" aria-pressed={mode === "latex"} onClick={() => setMode("latex")}>LaTeX</button>
    </div>
    {truncated && <p className="terminal-preview-notice" role="status">预览显示开头 {terminalPreviewLimit.toLocaleString()} 个字符。可缩小选区查看其余内容。</p>}
    {preview.invalid && <p className="terminal-preview-notice" role="status">{mode === "latex"
      ? "无法渲染该公式，请选取较短的完整 LaTeX 表达式。"
      : "当前显示选区原文。可缩小选区后重试。"}</p>}
    <div className="chat-md terminal-preview-content" tabIndex={0} aria-label="预览内容"
      data-testid="terminal-preview-content" dangerouslySetInnerHTML={{ __html: preview.html }} />
  </dialog>;
}
