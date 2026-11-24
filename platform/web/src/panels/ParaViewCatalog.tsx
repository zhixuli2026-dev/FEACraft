import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { activatePanel } from "../shell/dock";
import { captureProjectCase, useStore } from "../store";
import ParaViewInspector from "./ParaViewInspector";
import ParaViewCatalyst from "./ParaViewCatalyst";
import ParaViewPlugins from "./ParaViewPlugins";
import ParaViewPipelineEditor from "./ParaViewPipelineEditor";
import type { PipelineOutput } from "./ParaViewPipelineEditor";
import type { NativeProperty } from "./ParaViewInspector";
import "./ParaViewCatalog.css";

type Group = "sources" | "filters";
type Entry = { group: Group; name: string; label: string };
type Definition = Entry & {
  description?: string | null;
  properties: NativeProperty[];
};

const entryKey = (entry: Entry) => entry.group + "/" + entry.name;

export default function ParaViewCatalog({ caseId, pythonConsole, onView }: {
  caseId: number; pythonConsole: boolean; onView: (file: string) => void;
}) {
  const endpoint = "/cases/" + caseId + "/paraview/catalog";
  const [open, setOpen] = useState(false);
  const disclosure = useRef<HTMLDetailsElement | null>(null);
  const pipelineEditor = useRef<HTMLDetailsElement | null>(null);
  const [pipelineOutput, setPipelineOutput] = useState<PipelineOutput>("data");
  const [items, setItems] = useState<Entry[] | null>(null);
  const [operators, setOperators] = useState<Record<Group, string[]>>({ sources: [], filters: [] });
  const [group, setGroup] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState("");
  const [definition, setDefinition] = useState<Definition | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [retry, setRetry] = useState(0);
  const [inputPath, setInputPath] = useState("");
  const [inputMatches, setInputMatches] = useState<{ path: string; dataset: string; filters: Record<string, string[]> } | null>(null);
  const [inputError, setInputError] = useState("");
  const files = useStore(state => state.fileTree);
  const canConsole = useStore(state => Boolean(state.user && state.currentWs && !state.currentWs.shared &&
    state.currentWs.access_role !== "viewer" && state.currentWs.access_role !== "editor"));

  useEffect(() => {
    if (!open || items) return;
    const controller = new AbortController();
    void api.get<{ items: Entry[]; pipeline_operators: Record<Group,string[]> }>(endpoint, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) {
        setItems(result.items);
        setOperators(result.pipeline_operators);
      }
    }).catch(error => {
      if (!controller.signal.aborted) setCatalogError(String(error.message ?? error));
    });
    return () => controller.abort();
  }, [open, items, endpoint, retry]);

  useEffect(() => {
    if (!open || !selected) return;
    const controller = new AbortController();
    void api.get<Definition>(endpoint + "/" + selected, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) setDefinition(result);
    }).catch(error => {
      if (!controller.signal.aborted) setDetailError(String(error.message ?? error));
    });
    return () => controller.abort();
  }, [endpoint, open, selected, retry]);

  useEffect(() => {
    if (!open || !inputPath) return;
    const controller = new AbortController();
    void api.get<{ dataset: string; filters: Record<string, string[]> }>(`${endpoint}/inputs?path=${encodeURIComponent(inputPath)}`,
      undefined, controller.signal).then(value => {
        if (!controller.signal.aborted) setInputMatches({ ...value, path: inputPath });
      }).catch(error => {
        if (!controller.signal.aborted) setInputError(String(error.message ?? error));
      });
    return () => controller.abort();
  }, [endpoint, open, inputPath, retry]);

  const needle = query.trim().toLowerCase();
  const byInput = inputMatches?.path === inputPath ? inputMatches : null;
  const matches = (items ?? []).filter(item => (!group || item.group === group) && (!inputPath ||
    (item.group === "filters" && byInput?.filters[item.name])) &&
    [item.name, item.label].some(value => value.toLowerCase().includes(needle)));
  const detail = definition && entryKey(definition) === selected ? definition : null;
  const eligiblePorts = detail ? byInput?.filters[detail.name] : undefined;

  function select(value: string) {
    setSelected(value);
    setDetailError("");
  }

  function reload(setError: (message: string) => void) {
    setError("");
    setRetry(value => value + 1);
  }

  function openConsole() {
    const selection = captureProjectCase();
    if (!canConsole || !selection || selection.caseId !== caseId) return;
    useStore.getState().requestPythonConsole({ caseId, userId: selection.userId });
    activatePanel("terminal");
  }

  return (
    <details ref={disclosure} className="vf-pv-catalog" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>ParaView 来源与过滤器</summary>
      {(open || items) && <div className="vf-pv-content">
        {canConsole && pythonConsole && <button type="button" onClick={openConsole}>打开 Python 控制台</button>}
        <ParaViewCatalyst key={caseId} caseId={caseId} />
        <ParaViewPlugins caseId={caseId} onOpenPipeline={output => {
          setPipelineOutput(output);
          if (pipelineEditor.current) {
            pipelineEditor.current.open = true;
            pipelineEditor.current.querySelector("summary")?.focus();
          }
        }} onView={file => {
          if (disclosure.current) disclosure.current.open = false;
          onView(file);
        }} />
        <details className="vf-pv-extracts" ref={pipelineEditor}>
          <summary>管线编辑器</summary>
          <ParaViewPipelineEditor key={caseId} caseId={caseId} output={pipelineOutput} setOutput={setPipelineOutput}
            entries={(items ?? []).filter(item => operators[item.group].includes(item.name))} onView={file => {
              if (disclosure.current) disclosure.current.open = false;
              onView(file);
            }} />
        </details>
        {catalogError ? <div role="alert">
          <p>目录读取失败。{catalogError}</p>
          <button type="button" onClick={() => reload(setCatalogError)}>重新读取</button>
        </div> : !items ? <p role="status">正在读取 ParaView 定义…</p> : <>
          <div className="vf-pv-controls">
            <label>搜索<input type="search" value={query} placeholder="例如 Contour"
              onChange={event => { setQuery(event.target.value); select(""); }} /></label>
            <label>类型<select aria-label="类型" value={group} disabled={Boolean(inputPath)}
              onChange={event => { setGroup(event.target.value); select(""); }}>
              <option value="">全部</option><option value="sources">Sources</option><option value="filters">Filters</option>
            </select></label>
          </div>
          <label className="vf-pv-match-input">按输入数据筛选<select aria-label="按输入数据筛选" value={inputPath}
            onChange={event => {
              setInputPath(event.target.value); setInputMatches(null); setInputError("");
              setGroup(event.target.value ? "filters" : ""); select("");
            }}>
            <option value="">全部数据类型</option>
            {files.filter(file => file.kind !== "directory" && /\.(vtk|vtp|vtu|vti|vtr|vts|vtt)$/i.test(file.path))
              .map(file => <option key={file.path} value={file.path}>{file.path}</option>)}
          </select></label>
          {inputError && <p role="alert">无法核对输入域。{inputError} <button type="button"
            onClick={() => reload(setInputError)}>重试</button></p>}
          <p className="vf-pv-count" role="status">{matches.length} 个条目</p>
          <div className="vf-pv-split">
            {matches.length ? <select aria-label="目录条目" size={8} value={selected}
              onChange={event => select(event.target.value)}>
              <option value="" disabled>选择条目</option>
              {(["sources", "filters"] as const).map(kind => {
                const entries = matches.filter(entry => entry.group === kind);
                return entries.length ? <optgroup key={kind} label={kind === "sources" ? "Sources" : "Filters"}>
                  {entries.map(item => <option key={entryKey(item)} value={entryKey(item)}>{item.label}</option>)}
                </optgroup> : null;
              })}
            </select> : <p>{inputPath && !byInput && !inputError ? "正在核对原生输入域…" : "未找到匹配项，请换个搜索词。"}</p>}
            <section className="vf-pv-inspect" aria-label="原生属性">
              {detailError ? <div role="alert">
                <p>属性读取失败。{detailError}</p>
                <button type="button" onClick={() => reload(setDetailError)}>重试</button>
              </div> : detail ? <>
                <h3>{detail.label}</h3>
                <p><code>{detail.name}</code></p>
                {eligiblePorts?.length && <p>{byInput?.dataset} 可接入 {eligiblePorts.join("、")}</p>}
                {detail.description && <p>{detail.description}</p>}
                {detail.properties.length ? <dl>
                  {detail.properties.map(property => <div key={property.name}>
                    <dt><code>{property.name}</code></dt>
                    <dd>{property.type}
                      {property.attributes.default_values !== undefined && <span>默认值 {property.attributes.default_values}</span>}
                      {property.domains.length > 0 && <span>{property.domains.map(domain => domain.type).join(" · ")}</span>}
                    </dd>
                  </div>)}
                </dl> : <p>此定义没有声明可列出的属性。</p>}
              </> : <p role="status">{selected ? "正在读取属性…" : "选择条目，查看原生属性和默认值。"}</p>}
            </section>
          </div>
          {detail && operators[detail.group].includes(detail.name) && <ParaViewInspector key={selected+inputPath}
            caseId={caseId} group={detail.group} name={detail.name} properties={detail.properties}
            initialInput={eligiblePorts?.length === 1 ? { path: inputPath, port: eligiblePorts[0] } : undefined} onView={file => {
              if (disclosure.current) disclosure.current.open = false;
              onView(file);
            }} />}
        </>}
      </div>}
    </details>
  );
}
