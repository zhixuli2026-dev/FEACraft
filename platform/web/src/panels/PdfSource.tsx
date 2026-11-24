import { useEffect, useRef } from "react";
import "./ReportHistory.css";

export interface FrozenPdfSource { file: string; line: number; text: string; generation: number }

export default function PdfSource({source, close, reread}: {source: FrozenPdfSource; close: () => void; reread: () => void}) {
  const selected = useRef<HTMLSpanElement>(null);
  useEffect(() => { selected.current?.scrollIntoView({block:"center"}); }, [source]);
  return <section className="pdf-frozen-source" aria-label="发布版本源码" data-testid="pdf-frozen-source">
    <div className="pdf-source-heading"><span>版本 {source.generation} · {source.file}:{source.line} · 只读源码</span>
      <button type="button" onClick={reread}>重读所选源码</button>
      <button type="button" onClick={close}>关闭源码</button></div>
    <pre tabIndex={0} aria-label="只读历史源码">{source.text.split("\n").map((text,index) =>
      <span key={index} ref={index+1 === source.line ? selected : undefined}
        className={index+1 === source.line ? "pdf-source-current" : undefined}
        aria-current={index+1 === source.line ? "location" : undefined} data-line={index+1}>
        <span className="pdf-source-line" aria-hidden="true">{index+1}</span>{text}{"\n"}
      </span>)}</pre>
  </section>;
}
