import { useEffect, useState } from "react";
import { api } from "../api";
import ParaViewInspector from "./ParaViewInspector";
import type { NativeProperty } from "./ParaViewInspector";
import type { PipelineOutput } from "./ParaViewPipelineEditor";

type Plugin = { name: string; loaded: boolean; autoload: boolean; version: string | null; requires: string[] };
type Proxy = { name: string; label: string; group: string; description?: string; properties: NativeProperty[] };
type Detail = { plugin: Plugin; proxies: Proxy[]; pipeline_operators: string[]; reader_formats: Record<string, string[]>;
  render_modes?: { name: string; components?: number; projection?: boolean }[] };

export default function ParaViewPlugins({ caseId, onView, onOpenPipeline }: {
  caseId: number; onView: (path: string) => void; onOpenPipeline: (output: PipelineOutput) => void;
}) {
  const endpoint = `/cases/${caseId}/paraview/plugins`;
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Plugin[] | null>(null);
  const [name, setName] = useState("");
  const [proxy, setProxy] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const selected = detail?.plugin.name === name ? detail : null;
  const definition = selected?.proxies.find(item => `${item.group}/${item.name}` === proxy);
  const display = selected?.render_modes?.find(item => `view/${item.name}` === proxy);
  const volume = name === "AnalyzeNIfTIReaderWriter" && definition?.group === "writers"
    ? definition.name === "NIfTIWriter" ? "NIfTI" : definition.name === "AnalyzeWriter" ? "Analyze" : undefined : undefined;

  useEffect(() => {
    if (!open || items) return;
    const controller = new AbortController();
    void api.get<{ plugins: Plugin[] }>(endpoint, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) setItems(result.plugins);
    }).catch(cause => { if (!controller.signal.aborted) setError(String(cause.message ?? cause)); });
    return () => controller.abort();
  }, [endpoint, open, items, retry]);
  useEffect(() => {
    if (!open || !name) return;
    const controller = new AbortController();
    void api.get<Detail>(endpoint + "/" + encodeURIComponent(name), undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) setDetail(result);
    }).catch(cause => { if (!controller.signal.aborted) setError(String(cause.message ?? cause)); });
    return () => controller.abort();
  }, [endpoint, open, name, retry]);

  return <details className="vf-pv-extracts" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>ParaView 插件</summary>
    {open && <div className="vf-pv-editor">
      {items && <label>插件<select aria-label="ParaView 插件" value={name} onChange={event => {
        setName(event.target.value); setProxy(""); setDetail(null); setError("");
      }}>
        <option value="">选择已登记插件</option>
        {items.map(item => <option key={item.name}>{item.name}</option>)}
      </select></label>}
      {!error && (!items || (name && !selected)) && <p role="status">{items ? "正在装载插件并读取定义…" : "正在读取插件清单…"}</p>}
      {selected && <>
        <p role="status">装载通过 · {selected.plugin.version || "版本未声明"}
          {selected.plugin.autoload ? " · 自动装载" : " · 按需装载"}</p>
        {!!selected.plugin.requires.length && <p>依赖 {selected.plugin.requires.join("、")}</p>}
        {(selected.proxies.length > 0 || Boolean(selected.render_modes?.length)) && <label>插件条目<select aria-label="插件条目" value={proxy}
          onChange={event => setProxy(event.target.value)}>
          <option value="">选择原生条目</option>
          {selected.render_modes?.map(item => <option value={`view/${item.name}`} key={item.name}>{item.name}</option>)}
          {selected.proxies.map(item => <option value={`${item.group}/${item.name}`} key={`${item.group}/${item.name}`}>
            {item.label} · {item.group}
          </option>)}
        </select></label>}
        {display && <ParaViewInspector key={proxy} group="file" caseId={caseId} name={display.name}
          properties={[]} plugin={name} representationComponents={display.components} projection={display.projection} onView={onView} />}
        {name === "NodeEditor" && <button type="button" onClick={() => onOpenPipeline("data")}>打开管线编辑器</button>}
        {name === "SaveStateAndScreenshot" && <button type="button" onClick={() => onOpenPipeline("snapshot")}>保存状态与截图</button>}
        {name === "FlipBookPlugin" && <button type="button" onClick={() => onOpenPipeline("flipbook")}>对照播放</button>}
        {definition && <><p>{definition.description}</p>
          {volume ? <ParaViewInspector key={proxy} group="file" caseId={caseId} name={definition.name}
              properties={[]} plugin={name} volumeFormat={volume} onView={onView} />
            : (definition.group === "sources" || definition.group === "filters") &&
            (selected.pipeline_operators.includes(definition.name) || selected.reader_formats[definition.name])
            ? <ParaViewInspector key={proxy} caseId={caseId} group={definition.group} name={definition.name}
              properties={definition.properties} plugin={name} readerFormats={selected.reader_formats[definition.name]} onView={onView} />
            : <ul>{definition.properties.map(property => <li key={property.name}>{property.attributes.label || property.name}</li>)}</ul>}
        </>}
      </>}
      {error && <div role="alert"><p>{error}</p><button type="button" onClick={() => { setError(""); setRetry(value => value+1); }}>
        重试读取</button></div>}
    </div>}
  </details>;
}
