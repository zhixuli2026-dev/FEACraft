import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { captureProjectCase, useStore } from "../store";
import { downloadPipelineArtifact, submitPipeline } from "./ParaViewInspector";
import ParaViewPropertyFields, { editable, serializeProperties } from "./ParaViewPropertyFields";
import type { NativeProperty, PropertyDraft } from "./ParaViewPropertyFields";
import ParaViewAnimationFrames from "./ParaViewAnimationFrames";
import type { AnimationResult } from "./ParaViewAnimationFrames";
import { NativeResultImage } from "./ParaViewInspection";

export type PipelineEntry = { group: "sources" | "filters"; name: string; label: string };
export type PipelineOutput = "data" | "snapshot" | "flipbook";
type Definition = PipelineEntry & { properties: NativeProperty[] };
type Node = { id: string; definition?: Definition; file: string; inputs: Record<string,string[]>; values: PropertyDraft };
type Run = { job_id: number; report_path: string };
type Report = { result: string; state: string; nodes: { id: string; points: number; cells: number }[];
  animation?: AnimationResult; inspection?: { image: string } };
const keyOf = (entry: PipelineEntry) => `${entry.group}/${entry.name}`;
const inputPorts = (definition?: Definition) => definition?.properties.filter(property =>
  property.type === "InputProperty" && property.attributes.is_internal !== "1") ?? [];

export default function ParaViewPipelineEditor({ caseId, entries, output, setOutput, onView }: {
  caseId: number; entries: PipelineEntry[]; output: PipelineOutput; setOutput: (output: PipelineOutput) => void;
  onView: (path: string) => void;
}) {
  const canWrite = useStore(state => Boolean(state.currentWs && state.currentWs.access_role !== "viewer"));
  const tree = useStore(state => state.fileTree);
  const files = tree.filter(file => file.kind !== "directory" && /\.(vtk|vtp|vtu|vti|vtr|vts|vtt)$/i.test(file.path));
  const [nodes, setNodes] = useState<Node[]>([]);
  const [selected, setSelected] = useState("");
  const [operator, setOperator] = useState("file");
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState<Run | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [poll, setPoll] = useState(0);
  const [flipbook, setFlipbook] = useState<string[]>([]);
  const [fps, setFps] = useState("2");
  const [downloading, setDownloading] = useState(false);
  const nextId = useRef(0);
  const operation = useRef<AbortController | null>(null);
  const download = useRef<AbortController | null>(null);
  const pending = useRef<{ body: string; key: string } | null>(null);
  const index = nodes.findIndex(node => node.id === selected);
  const active = nodes[index];
  const earlier = nodes.slice(0,index);
  const locked = busy || Boolean(running) || !canWrite;

  useEffect(() => () => { operation.current?.abort(); download.current?.abort(); }, []);
  useEffect(() => {
    if (!running) return;
    const scope = captureProjectCase();
    if (scope?.caseId !== caseId) return;
    const controller = new AbortController();
    let timer = 0;
    const current = () => !controller.signal.aborted && scope.isCurrent();
    async function read() {
      if (!running) return;
      try {
        const job = await api.get<{ case_id: number; status: string; completion_pending: boolean; log_backlog?: string;
          recovery?: { state: string } }>(
          `/jobs/${running.job_id}`,undefined,controller.signal);
        if (!current()) return;
        if (job.case_id !== caseId) throw new Error("作业不属于此算例");
        if (job.recovery?.state === "requires_attention") throw new Error("作业等待恢复，可在作业面板查看详情。");
        if (job.status === "queued" || job.status === "running" || job.completion_pending) {
          timer = window.setTimeout(() => void read(),750);
          return;
        }
        if (job.status === "succeeded") {
          const output = await api.get<Report>(`/cases/${caseId}/raw?path=${encodeURIComponent(running.report_path)}`,
            undefined,controller.signal);
          if (!current()) return;
          setReport(output);
          void useStore.getState().loadTree().catch(() => {});
        } else setError(job.log_backlog?.slice(-1600) || `ParaView 作业 ${job.status}`);
        setRunning(null);
      } catch (cause) {
        if (current()) setError(String(cause instanceof Error ? cause.message : cause));
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [caseId, running, poll]);

  async function add() {
    if (locked || operation.current || nodes.length >= 32) return;
    const scope = captureProjectCase();
    if (scope?.caseId !== caseId) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError("");
    try {
      const definition = operator === "file" ? undefined : await api.get<Definition>(
        `/cases/${caseId}/paraview/catalog/${operator}`,undefined,controller.signal);
      if (!scope.isCurrent() || controller.signal.aborted) return;
      const id = `node_${++nextId.current}`;
      const inputs = definition?.group === "filters" ? Object.fromEntries(inputPorts(definition)
        .filter(property => property.attributes.optional !== "1" && nodes.length)
        .map(property => [property.name,[nodes[nodes.length-1].id]])) : {};
      setNodes(previous => [...previous,{id,definition,file:"",inputs,values:{}}]);
      setSelected(id);
    } catch (cause) {
      if (scope.isCurrent() && !controller.signal.aborted) setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      operation.current = null;
      if (scope.isCurrent() && !controller.signal.aborted) setBusy(false);
    }
  }

  function update(changes: Partial<Node>) {
    setNodes(nodes.map(node => node.id === selected ? { ...node, ...changes } : node));
  }

  function remove() {
    setNodes(nodes.filter(node => node.id !== selected).map(node => ({ ...node,
      inputs: Object.fromEntries(Object.entries(node.inputs).map(([name, refs]) => [name,refs.filter(ref => ref !== selected)])) })));
    setSelected(nodes[index-1]?.id || nodes[index+1]?.id || "");
    setFlipbook(flipbook.filter(id => id !== selected));
  }

  async function save(path: string) {
    const scope = captureProjectCase();
    if (download.current || scope?.caseId !== caseId) return;
    const controller = new AbortController();
    download.current = controller;
    setDownloading(true);
    setError("");
    try {
      await downloadPipelineArtifact(caseId,path,controller.signal,scope.isCurrent);
    } catch (cause) {
      if (!controller.signal.aborted && scope.isCurrent()) setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      download.current = null;
      if (!controller.signal.aborted && scope.isCurrent()) setDownloading(false);
    }
  }

  async function run() {
    if (locked || operation.current) return;
    const scope = captureProjectCase();
    if (scope?.caseId !== caseId) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(true);
    setError("");
    try {
      const body = { nodes:nodes.map(node => {
        const definition = node.definition;
        if (!definition) return { id:node.id,group:"file",file:node.file };
        return { id:node.id,group:definition.group,name:definition.name,
          inputs:Object.fromEntries(Object.entries(node.inputs).filter(([,refs]) => refs.length)),
          properties:serializeProperties(definition.properties.filter(editable),node.values) };
      }), inspection: output === "snapshot" ? {} : undefined,
        animation: output === "flipbook" ? { flipbook, fps:Number(fps) } : undefined };
      const signature = JSON.stringify(body);
      if (pending.current?.body !== signature) pending.current = { body:signature,key:crypto.randomUUID() };
      const receipt = await submitPipeline(caseId,body,pending.current.key,controller.signal);
      if (!controller.signal.aborted && scope.isCurrent()) {
        pending.current = null;
        setRunning(receipt);
      }
    } catch (cause) {
      if (!controller.signal.aborted && scope.isCurrent()) setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted && scope.isCurrent()) setBusy(false);
    }
  }

  const runnable = nodes.length > 0 && nodes.every(node => node.definition
    ? inputPorts(node.definition).every(port => port.attributes.optional === "1" || node.inputs[port.name]?.length)
    : files.some(file => file.path === node.file));
  const nodeName = (node: Node) => node.definition?.label || node.file.split("/").at(-1) || "算例文件";
  const flipbookReady = flipbook.length >= 2 && Number(fps) >= .1 && Number(fps) <= 60;
  const snapshot = report?.inspection?.image;

  return <section className="vf-pv-editor vf-pv-node-editor" aria-label="管线编辑器">
    <div className="vf-pv-node-add">
      <label>新节点<select aria-label="新节点" value={operator} onChange={event => setOperator(event.target.value)} disabled={locked}>
        <option value="file">算例文件</option>
        {(["sources","filters"] as const).map(group => <optgroup key={group} label={group === "sources" ? "来源" : "过滤器"}>
          {entries.filter(entry => entry.group === group).map(entry => <option key={keyOf(entry)} value={keyOf(entry)} disabled={group === "filters" && !nodes.length}>{entry.label}</option>)}
        </optgroup>)}
      </select></label>
      <button type="button" disabled={locked || nodes.length >= 32 || (!nodes.length && operator.startsWith("filters/"))}
        onClick={() => void add()}>添加节点</button>
    </div>
    {nodes.length > 0 && <div className="vf-pv-node-grid">
      <ol aria-label="已连接的管线节点">{nodes.map(node => {
        const sources = Array.from(new Set(Object.values(node.inputs).flat()));
        return <li key={node.id}><button type="button" aria-pressed={node.id === selected} onClick={() => setSelected(node.id)}>
          {nodeName(node)}{sources.length > 0 && <span>来自 {sources.map(ref => nodes.find(item => item.id===ref)).filter(item => item !== undefined).map(nodeName).join("、")}</span>}
        </button></li>;
      })}</ol>
      {active && <div className="vf-pv-node-properties">
        <h4>{nodeName(active)}</h4>
        {!active.definition && <label>输入数据集<select aria-label="输入数据集" value={active.file} disabled={locked}
          onChange={event => update({ file:event.target.value })}>
          <option value="">选择 VTK 文件</option>
          {files.map(file => <option key={file.path} value={file.path}>{file.path}</option>)}
        </select></label>}
        {inputPorts(active.definition).map(port => <label key={port.name}>{port.attributes.label || port.name}
          <select aria-label={port.attributes.label || port.name} value={port.multiple ? active.inputs[port.name] ?? [] : active.inputs[port.name]?.[0] ?? ""}
            multiple={port.multiple} disabled={locked} onChange={event => update({ inputs:{...active.inputs,
              [port.name]:Array.from(event.target.selectedOptions,option => option.value).filter(Boolean)} })}>
            {!port.multiple && <option value="">选择前置节点</option>}
            {earlier.map(node => <option key={node.id} value={node.id}>{nodeName(node)}</option>)}
          </select></label>)}
        <ParaViewPropertyFields fields={(active.definition?.properties ?? []).filter(editable)} values={active.values}
          disabled={locked} hasInput={earlier.length > 0} update={values => update({ values })} />
        <button type="button" disabled={locked} onClick={remove}>移除节点</button>
      </div>}
    </div>}
    {nodes.length > 0 && <div className="vf-pv-node-output">
      <label>输出<select aria-label="输出" value={output} disabled={locked} onChange={event => setOutput(event.target.value as PipelineOutput)}>
        <option value="data">数据与状态</option><option value="snapshot">状态与截图</option><option value="flipbook">对照播放</option>
      </select></label>
      {output === "flipbook" && <><label>对照节点<select aria-label="对照节点" value={flipbook} multiple disabled={locked}
        onChange={event => setFlipbook(Array.from(event.target.selectedOptions,option => option.value))}>
        {nodes.map(node => <option key={node.id} value={node.id}>{nodeName(node)}</option>)}
      </select></label><label>每秒帧数<input aria-label="每秒帧数" type="number" min={.1} max={60} step="any" value={fps}
        disabled={locked} onChange={event => setFps(event.target.value)} /></label></>}
    </div>}
    <div className="vf-pv-actions">
      <button type="button" disabled={locked || !runnable || (output === "flipbook" && !flipbookReady)} onClick={() => void run()}>运行管线</button>
      {report && <button type="button" onClick={() => onView(report.result)}>查看管线结果</button>}
      {report && <button type="button" disabled={downloading} onClick={() => void save(report.state)}>下载状态</button>}
      {snapshot && <button type="button" disabled={downloading} onClick={() => void save(snapshot)}>下载截图</button>}
    </div>
    {running && <p role="status">管线作业 #{running.job_id}</p>}
    {report && <p role="status">结果包含 {report.nodes.at(-1)?.points} 个点和 {report.nodes.at(-1)?.cells} 个单元</p>}
    {snapshot && <div className="vf-pv-inspection-result"><NativeResultImage key={snapshot}
      caseId={caseId} path={snapshot} label="ParaView 管线截图" width={800} height={600} /></div>}
    {report?.animation && <ParaViewAnimationFrames key={report.state} caseId={caseId} animation={report.animation} />}
    {error && <div role="alert"><p>{error}</p>{running && <button type="button" onClick={() => {setError("");setPoll(poll+1);}}>重查作业</button>}</div>}
  </section>;
}
