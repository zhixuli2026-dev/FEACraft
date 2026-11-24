import { useEffect, useRef, useState } from "react";
import { fetchDocumentFile } from "../utils/documentSnapshot";
import type { PptRebuild } from "../utils/pptImageRebuild";

type Props = {
  caseId: number;
  imagePaths: string[];
  value: PptRebuild | null;
  onChange: (value: PptRebuild | null) => void;
  onBusy: (busy: boolean) => void;
};

export default function PptImageRebuild({ caseId, imagePaths, value, onChange, onBusy }: Props) {
  const [language, setLanguage] = useState<"eng" | "chi_sim">("eng");
  const [local, setLocal] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => { active.current?.abort(); active.current = null; }, []);

  const clear = () => {
    active.current?.abort();
    active.current = null;
    setBusy(false);
    onBusy(false);
    onChange(null);
    setMessage("");
  };

  const run = async () => {
    if (active.current || (!local && !source)) return;
    const run = new AbortController();
    const signal = AbortSignal.any([run.signal, AbortSignal.timeout(90000)]);
    active.current = run;
    setBusy(true);
    onBusy(true);
    onChange(null);
    setMessage("正在本地识别文字和图形…");
    try {
      const file = local ?? await (await fetchDocumentFile(caseId, source, signal)).blob();
      const { recognizePptImage } = await import("../utils/pptImageRebuild");
      const result = await recognizePptImage(file, language, signal);
      if (active.current !== run) return;
      if (!result.text_regions.length && !result.shapes.length) throw new Error("未检测到可编辑的文字或图形");
      onChange(result);
      setMessage(`识别出 ${result.text_regions.length} 段文字和 ${result.shapes.length} 个图形`);
    } catch (error) {
      if (active.current === run) setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (active.current === run) {
        active.current = null;
        setBusy(false);
        onBusy(false);
      }
    }
  };

  return <details>
    <summary>从图片重建可编辑 PPT</summary>
    <div style={{ display: "grid", gap: 6, paddingBlock: 6, fontSize: 12 }}>
      <label>算例图片 <select aria-label="PPT 重建算例图片" value={source} onChange={(event) => {
        clear(); setLocal(null); setSource(event.target.value);
      }}>
        <option value="">选择图片…</option>
        {imagePaths.map((path) => <option key={path} value={path}>{path}</option>)}
      </select></label>
      <label>本地图片 <input aria-label="PPT 重建本地图片" type="file" accept="image/png,image/jpeg,image/webp"
        onChange={(event) => {
          clear(); setSource(""); setLocal(event.target.files?.[0] ?? null); event.target.value = "";
        }} />{local && <span>{local.name}</span>}</label>
      <label>识别语言 <select value={language} onChange={(event) => {
        clear(); setLanguage(event.target.value === "chi_sim" ? "chi_sim" : "eng");
      }}><option value="eng">英语</option><option value="chi_sim">简体中文</option></select></label>
      <div style={{ display: "flex", gap: 6 }}>
        <button type="button" onClick={run} disabled={busy || (!source && !local)}>识别文字和图形</button>
        {(busy || value) && <button type="button" onClick={clear}>{busy ? "取消识别" : "清除重建结果"}</button>}
      </div>
      {message && <div role="status" data-testid="ppt-rebuild-status">{message}</div>}
      {value && <div style={{ display: "grid", gap: 4, maxHeight: 190, overflow: "auto" }}>
        {value.text_regions.map((item, index) =>
          <input key={index} aria-label={`重建文字 ${index + 1}`} value={item.text} maxLength={800}
            onChange={(event) => onChange({ ...value, text_regions: value.text_regions.map((region, i) =>
              i === index ? { ...region, text: event.target.value } : region) })} />)}
      </div>}
    </div>
  </details>;
}
