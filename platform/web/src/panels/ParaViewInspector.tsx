import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { captureProjectCase, useStore } from "../store";
import ParaViewAnimationFrames from "./ParaViewAnimationFrames";
import type { AnimationResult } from "./ParaViewAnimationFrames";
import { defaultInspection, InspectionPreview, InspectionSettings, inspectionParameters, NativeResultImage } from "./ParaViewInspection";
import type { InspectionDraft, InspectionResult } from "./ParaViewInspection";
import ParaViewPropertyFields, { arrayField, editable, serializeProperties } from "./ParaViewPropertyFields";
import type { ArrayInfo, NativeProperty, PropertyDraft } from "./ParaViewPropertyFields";

export type { NativeProperty } from "./ParaViewPropertyFields";
const formats = ["VTK", "CSV", "PNG"] as const;
type Format = typeof formats[number];
type Draft = { file: string; displayField: string; projectionAngle: string; streaming: boolean; inputs: Record<string, string[]>;
  values: PropertyDraft; animation: string; formats: Format[]; inspection: InspectionDraft };
type Extract = { rule: { id: string; format: Format }; frames: { path: string; step: number; time: number }[]; files: string[] };
type Report = { result: string; state: string; nodes: { cells: number; points: number }[];
  animation?: AnimationResult; extractors?: Extract[]; inspection?: InspectionResult; chart?: string;
  volume?: { format: string; files: string[] } };
type Run = { job_id: number; report_path: string };
type Job = { id: number; case_id: number; status: string; log_backlog?: string; recovery?: { state: string } };
type Version = { report: Report; draft: Draft; jobId: number };
const dataset = /\.(vtk|vtp|vtu|vti|vtr|vts|vtt)$/i;
const emptyDraft: Draft = { file: "", displayField: "", projectionAngle: "360", streaming: false,
  inputs: {}, values: {}, animation: "", formats: [], inspection: defaultInspection };

export async function submitPipeline(caseId: number, body: unknown, key: string, signal: AbortSignal) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await api.post<Run>("/cases/" + caseId + "/paraview/pipeline", body, key, signal);
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.code !== "storage_quota_busy" || signal.aborted || attempt >= 5) throw cause;
    }
    await new Promise<void>(resolve => setTimeout(resolve, 250 + attempt * 150));
    signal.throwIfAborted();
  }
}

export async function downloadPipelineArtifact(caseId: number, path: string, signal: AbortSignal, isCurrent: () => boolean) {
  const token = localStorage.getItem("vf_token");
  const response = await fetch(`/api/v1/cases/${caseId}/raw?path=${encodeURIComponent(path)}`,
    { signal, headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!response.ok) throw new Error(`文件读取失败（HTTP ${response.status}）`);
  const blob = await response.blob();
  if (signal.aborted || !isCurrent()) return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = path.split("/").at(-1) || "artifact";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}

export default function ParaViewInspector({ caseId, group, name, properties, plugin, readerFormats, representationComponents, projection, volumeFormat, initialInput, onView }: {
  caseId: number; group: "sources" | "filters" | "file"; name: string; properties: NativeProperty[]; plugin?: string;
  representationComponents?: number;
  projection?: boolean;
  volumeFormat?: "NIfTI" | "Analyze";
  readerFormats?: string[]; initialInput?: { path: string; port: string }; onView: (file: string) => void;
}) {
  const canWrite = useStore(state => Boolean(state.currentWs && state.currentWs.access_role !== "viewer"));
  const tree = useStore(state => state.fileTree);
  const [draft, setDraft] = useState<Draft>(() => initialInput ? { ...emptyDraft, inputs: { [initialInput.port]: [initialInput.path] } } : emptyDraft);
  const [active, setActive] = useState<{ run: Run; draft: Draft } | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [pollAgain, setPollAgain] = useState(0);
  const [datasetInfo, setDatasetInfo] = useState<{ file: string; arrays: ArrayInfo[] } | null>(null);
  const [datasetError, setDatasetError] = useState("");
  const request = useRef<{ body: unknown; key: string } | null>(null);
  const action = useRef<AbortController | null>(null);
  const download = useRef<AbortController | null>(null);
  const [downloading, setDownloading] = useState("");
  const ready = versions.at(-1);
  const isFilter = group === "filters";
  const particleSource = plugin === "StreamingParticles" && name === "RandomPoints";
  const displayMode = Boolean(representationComponents || projection);
  const needsFile = Boolean(readerFormats || displayMode || volumeFormat);
  const needsDatasetInfo = isFilter || Boolean(representationComponents);
  const needsTimeSeries = name === "TemporalStatistics";
  const fields = readerFormats ? [] : properties.filter(editable);
  const ports = isFilter ? properties.filter(property => property.type === "InputProperty" && property.attributes.is_internal !== "1") : [];
  const primaryInput = representationComponents ? draft.file : ports.length ? draft.inputs[ports[0].name]?.[0] || "" : "";
  const files = tree.filter(file => file.kind !== "directory" && (readerFormats
    ? readerFormats.some(extension => file.path.toLowerCase().endsWith(extension))
    : volumeFormat ? /\.(vti|vtk|nii(?:\.gz)?|hdr)$/i.test(file.path) : dataset.test(file.path))).map(file => file.path);
  const busy = submitting || Boolean(active);

  useEffect(() => () => { action.current?.abort(); download.current?.abort(); }, []);
  useEffect(() => {
    const file = primaryInput;
    if (!needsDatasetInfo || !file) return;
    const controller = new AbortController();
    void api.get<{ arrays: ArrayInfo[] }>("/cases/" + caseId + "/paraview/dataset?path=" + encodeURIComponent(file),
      undefined, controller.signal).then(report => {
      if (!controller.signal.aborted) setDatasetInfo({ file, arrays: report.arrays });
    }).catch(cause => {
      if (!controller.signal.aborted) setDatasetError(String(cause instanceof Error ? cause.message : cause));
    });
    return () => controller.abort();
  }, [caseId, primaryInput, needsDatasetInfo]);
  useEffect(() => {
    const selection = captureProjectCase();
    if (!active || !selection || selection.caseId !== caseId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const current = () => !controller.signal.aborted && selection.isCurrent();
    const poll = async () => {
      try {
        const { run, draft: submitted } = active;
        const job = await api.get<Job>("/jobs/" + run.job_id, undefined, controller.signal);
        if (!current()) return;
        if (job.id !== run.job_id || job.case_id !== caseId) throw new Error("作业与当前算例不匹配");
        if (job.recovery?.state === "requires_attention") {
          setError("作业等待恢复，可在作业面板查看详情。");
          return;
        }
        if (job.status === "queued" || job.status === "running") {
          timer = setTimeout(() => void poll(), 750);
          return;
        }
        if (job.status === "succeeded") {
          const report = await api.get<Report>("/cases/" + caseId + "/raw?path=" + encodeURIComponent(run.report_path),
            undefined, controller.signal);
          if (!current()) return;
          setVersions(history => [...history, { report, draft: submitted, jobId: job.id }]);
          void useStore.getState().loadTree().catch(() => {});
        } else {
          const detail = job.log_backlog?.match(/(?:ValueError|RuntimeError): ([^\n]+)/)?.[1];
          setError(detail ? "应用失败。" + detail : "作业已结束。可在作业面板查看日志。");
        }
        setActive(null);
      } catch (cause) {
        if (current()) setError("读取结果失败。" + String(cause instanceof Error ? cause.message : cause));
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, caseId, pollAgain]);

  async function apply() {
    const selection = captureProjectCase();
    if (!canWrite || busy || action.current || selection?.caseId !== caseId) return;
    let body: unknown;
    try {
      const values = serializeProperties(fields, draft.values);
      const files = Array.from(new Set(Object.values(draft.inputs).flat().filter(Boolean)));
      const references = new Map(files.map((file, index) => [file, index ? `dataset_${index + 1}` : "dataset"]));
      const datasets = needsTimeSeries ? [{ id: "dataset", group: "file", files }]
        : files.map(file => ({ id: references.get(file), group: "file", file }));
      const inputs = needsTimeSeries ? { Input: ["dataset"] }
        : Object.fromEntries(Object.entries(draft.inputs).filter(([, paths]) => paths.some(Boolean))
          .map(([port, paths]) => [port, paths.filter(Boolean).map(file => references.get(file))]));
      const node = { id: "edited", group, name, properties: values, ...(isFilter ? { inputs } : {}) };
      const animation = particleSource && draft.streaming ? { streaming: 16, fps: 2 }
        : draft.animation.trim() ? JSON.parse(draft.animation) : undefined;
      const extractors = draft.formats.map(format => ({ id: format.toLowerCase(), node: "edited", format }));
      const reader = readerFormats ? name : volumeFormat && /\.nii(?:\.gz)?$/i.test(draft.file) ? "NIfTIReader"
        : volumeFormat && /\.hdr$/i.test(draft.file) ? "AnalyzeReader" : undefined;
      const selected = needsFile ? [{ id: "edited", group: "file", file: draft.file, ...(reader ? { reader } : {}) }]
        : [...datasets, node];
      let inspection: unknown;
      if (representationComponents) inspection = { representation: { mode: name, array: JSON.parse(draft.displayField) } };
      else if (projection) inspection = { projection: { mode: name, angle: Number(draft.projectionAngle) } };
      else inspection = inspectionParameters(draft.inspection);
      body = { nodes: selected, animation,
        extractors: extractors.length ? extractors : undefined, inspection,
        plugins: plugin ? [plugin] : undefined, volume_format: volumeFormat };
    } catch (cause) {
      setError("检查属性值。" + String(cause instanceof Error ? cause.message : cause));
      return;
    }
    if (!request.current || JSON.stringify(request.current.body) !== JSON.stringify(body)) {
      request.current = { body, key: crypto.randomUUID() };
    }
    const controller = new AbortController();
    action.current = controller;
    setSubmitting(true);
    setError("");
    try {
      const run = await submitPipeline(caseId, body, request.current.key, controller.signal);
      if (!controller.signal.aborted && selection.isCurrent()) {
        request.current = null;
        setActive({ run, draft });
      }
    } catch (cause) {
      if (!controller.signal.aborted && selection.isCurrent()) {
        setError("提交失败。" + String(cause instanceof Error ? cause.message : cause));
      }
    } finally {
      action.current = null;
      if (!controller.signal.aborted && selection.isCurrent()) setSubmitting(false);
    }
  }

  function undo() {
    const previous = versions.at(-2);
    if (!previous || busy) return;
    setVersions(versions.slice(0, -1));
    setDraft(previous.draft);
    setError("");
  }

  async function downloadExtract(path: string) {
    const selection = captureProjectCase();
    if (download.current || !selection || selection.caseId !== caseId) return;
    const controller = new AbortController();
    download.current = controller;
    setDownloading(path);
    setError("");
    try {
      await downloadPipelineArtifact(caseId,path,controller.signal,selection.isCurrent);
    } catch (cause) {
      if (!controller.signal.aborted && selection.isCurrent()) setError(String(cause instanceof Error ? cause.message : cause));
    } finally {
      download.current = null;
      if (!controller.signal.aborted && selection.isCurrent()) setDownloading("");
    }
  }

  const result = ready?.report.nodes.at(-1);
  const chart = ready?.report.chart;
  const arrays = datasetInfo?.file === primaryInput ? datasetInfo.arrays : null;
  const displayArrays = arrays?.filter(array => array.components === representationComponents &&
    (array.association === "点" || array.association === "单元")) ?? [];
  const fieldLabel = representationComponents === 3 ? "矢量场" : "标量场";
  const requiredInputs = ports.every(port => {
    const paths = draft.inputs[port.name] ?? [];
    return !paths.some(path => path && !files.includes(path))
      && (port.attributes.optional === "1" || paths.some(path => files.includes(path)));
  });
  const timeFiles = needsTimeSeries ? (draft.inputs.Input ?? []).filter(Boolean) : [];
  const timeSeriesReady = !needsTimeSeries || (new Set(timeFiles).size === timeFiles.length && timeFiles.length >= 2
    && new Set(timeFiles.map(path => path.split(".").at(-1)?.toLowerCase())).size === 1);
  const updateInput = (port: string, paths: string[]) => {
    setDatasetError("");
    const arrayFields = new Set(fields.filter(arrayField).map(property => property.name));
    setDraft({ ...draft, inputs: { ...draft.inputs, [port]: paths }, values: Object.fromEntries(
      Object.entries(draft.values).filter(([property]) => !arrayFields.has(property))) });
  };
  return <form className="vf-pv-editor" onSubmit={event => { event.preventDefault(); void apply(); }}>
    <h4>{volumeFormat ? "导出体数据" : displayMode ? "显示设置" : "应用属性"}</h4>
    {needsFile && <label>输入数据集<select aria-label="输入数据集" value={draft.file} disabled={busy || !canWrite}
      onChange={event => { setDatasetError(""); setDraft({ ...draft, file: event.target.value, displayField: "" }); }}>
      <option value="" disabled>选择 {readerFormats?.join(" / ") || (volumeFormat ? "VTI、NIfTI 或 Analyze" : "VTK")} 文件</option>
      {files.map(file => <option key={file} value={file}>{file}</option>)}
    </select></label>}
    {volumeFormat === "Analyze" && <p>Analyze 需要等间距的单分量体数据，非等距数据可导出为 NIfTI。</p>}
    {projection && <label>全景角度（°）<input type="number" required min={90} max={360} step="any"
      value={draft.projectionAngle} disabled={busy || !canWrite}
      onChange={event => setDraft({ ...draft, projectionAngle: event.target.value })} /></label>}
    {representationComponents && <label>{fieldLabel}<select aria-label={fieldLabel} value={draft.displayField}
      disabled={busy || !canWrite || !displayArrays.length} onChange={event => setDraft({ ...draft, displayField: event.target.value })}>
      <option value="">{!draft.file ? "先选择数据集" : arrays && !displayArrays.length ? `没有可用的${fieldLabel}` : `选择${fieldLabel}`}</option>
      {displayArrays.map(array => {
        const value = JSON.stringify([array.association === "点" ? "POINTS" : "CELLS", array.name]);
        return <option key={value} value={value}>{array.name} · {array.association}</option>;
      })}
    </select></label>}
    {ports.map(port => {
      const paths = draft.inputs[port.name] || [""];
      const multiple = port.multiple || needsTimeSeries;
      const label = needsTimeSeries ? "时间序列" : ports.length === 1 && !port.multiple ? "输入数据集" : (port.attributes.label || port.name);
      return <fieldset key={port.name} className="vf-pv-connections" disabled={busy || !canWrite}>
        <legend>{label}</legend>
        {paths.map((path, index) => {
          const fieldLabel = multiple ? `${label} ${index + 1}` : label;
          return <div className="vf-pv-connection" key={index}>
            <select aria-label={fieldLabel} value={path}
              onChange={event => updateInput(port.name, paths.map((old, i) => i === index ? event.target.value : old))}>
              <option value="">选择 VTK 数据集</option>
              {files.map(file => <option key={file} value={file}>{file}</option>)}
            </select>
            {paths.length > 1 && <button type="button" aria-label={`移除 ${fieldLabel}`}
              onClick={() => updateInput(port.name, paths.filter((_, i) => i !== index))}>移除</button>}
          </div>;
        })}
        {multiple && paths.length < (needsTimeSeries ? 256 : 31) && <button type="button" onClick={() => updateInput(port.name,[...paths,""])}>
          {needsTimeSeries ? "添加时间步" : "添加输入"}
        </button>}
        {needsTimeSeries && !timeSeriesReady && <p>按时间顺序选择至少两个同格式文件。</p>}
      </fieldset>;
    })}
    {datasetError && <p role="alert">读取输入字段失败。{datasetError}</p>}
    {!displayMode && !volumeFormat && <><ParaViewPropertyFields fields={fields} values={draft.values} disabled={busy || !canWrite}
      arrays={isFilter ? arrays : undefined} hasInput={Boolean(primaryInput)}
      update={values => setDraft({ ...draft, values })} />
    {particleSource && <label className="vf-pv-streaming"><input type="checkbox" checked={draft.streaming}
      disabled={busy || !canWrite} onChange={event => setDraft({ ...draft, streaming: event.target.checked })} />按块载入预览</label>}
    {!draft.streaming && <details className="vf-pv-animation-edit">
      <summary>属性关键帧</summary>
      <label>属性关键帧 JSON
        <textarea aria-label="属性关键帧 JSON" rows={4} value={draft.animation} disabled={busy || !canWrite}
          placeholder='{"fps":4,"tracks":[{"node":"edited","property":"Radius","frames":[{"t":0,"value":0.5},{"t":1,"value":1.5}]}]}'
          onChange={event => setDraft({ ...draft, animation: event.target.value })} />
      </label>
    </details>}
    <details className="vf-pv-extracts">
      <summary>提取器</summary>
      <fieldset disabled={busy || !canWrite}>
        <legend>输出格式</legend>
        {formats.map(format => <label key={format}>
          <input type="checkbox" checked={draft.formats.includes(format)} onChange={event => setDraft({ ...draft,
            formats: event.target.checked ? [...draft.formats, format] : draft.formats.filter(item => item !== format) })} />
          {format}
        </label>)}
      </fieldset>
    </details>
    <InspectionSettings draft={draft.inspection} locked={!canWrite || busy}
      fields={fields.filter(field => ["IntVectorProperty", "DoubleVectorProperty"].includes(field.type)
        && field.attributes.number_of_elements === "1" && field.attributes.repeat_command !== "1"
        && !field.domains.some(domain => domain.type === "EnumerationDomain" || domain.type === "BooleanDomain"))
        .map(field => ({ name: field.name, label: field.attributes.label || field.name }))}
      update={inspection => setDraft({ ...draft, inspection })} /></>}
    <div className="vf-pv-actions">
      <button type="submit" disabled={!canWrite || busy || !requiredInputs || !timeSeriesReady ||
        (needsFile && !files.includes(draft.file)) || Boolean(representationComponents && !draft.displayField)}>
        {busy ? "应用中…" : volumeFormat ? "导出" : displayMode ? "生成预览" : "应用并生成结果"}
      </button>
      <button type="button" disabled={busy || versions.length < 2} onClick={undo}>撤销应用</button>
      <button type="button" disabled={!ready || !result?.cells || !/\.(vtk|vtp|vtu|vti|vtr|vts|vtm)$/i.test(ready.report.result)}
        onClick={() => ready && onView(ready.report.result)}>查看结果</button>
    </div>
    {active && <p role="status">ParaView 作业 #{active.run.job_id}</p>}
    {result && <p role="status">作业 #{ready?.jobId} · {result.points} 个点 · {result.cells} 个单元</p>}
    {ready?.report.animation?.frames.length ? <ParaViewAnimationFrames key={ready.jobId} caseId={caseId}
      animation={ready.report.animation} /> : null}
    {ready?.report.inspection && <InspectionPreview key={ready.jobId} caseId={caseId} value={ready.report.inspection}
      downloadMaterial={downloadExtract} downloading={Boolean(downloading)} />}
    {chart && <section className="vf-pv-inspection-result" aria-label="Bag Plot 统计图">
      <NativeResultImage key={chart} caseId={caseId} path={chart} label="Bag Plot 统计图" width={900} height={560} />
      <button type="button" disabled={Boolean(downloading)} onClick={() => void downloadExtract(chart)}>
        {downloading ? "正在下载…" : "下载统计图"}
      </button>
    </section>}
    {ready?.report.volume && <div className="vf-pv-actions" aria-label="体数据原件">
      {ready.report.volume.files.map(path => <button type="button" key={path} disabled={Boolean(downloading)}
        onClick={() => void downloadExtract(path)}>下载 {path.split("/").at(-1)}</button>)}
    </div>}
    {ready?.report.extractors?.map(extract => <details className="vf-pv-extracts" key={extract.rule.id}>
      <summary>{extract.rule.format} · {extract.frames.length} 个时间步</summary>
      <ul>{extract.files.map(path => <li key={path}>
        <button type="button" disabled={Boolean(downloading)} onClick={() => void downloadExtract(path)}>
          {downloading === path ? "正在下载…" : `下载 ${path.split("/").at(-1)}`}
        </button>
      </li>)}</ul>
    </details>)}
    {error && <div role="alert"><p>{error}</p>
      {active && <button type="button" onClick={() => { setError(""); setPollAgain(x => x + 1); }}>重查作业</button>}</div>}
  </form>;
}
