import { Fragment, useEffect, useState } from "react";
import { api } from "../api";
import "./CaseIntentHistory.css";

type Publication = {
  id: string;
  job_id: number;
  kind: string;
  generation: number;
  status: "committed" | "superseded";
  active: boolean;
};
type Page = { items: Publication[]; next_before_id: string | null };
type Trace = {
  publication: Publication & { manifest_sha256: string };
  source: {
    case_dict: { revision?: number; sha256?: string } | null;
    input_publications: { publication_id: string; kind: string; generation: number }[];
  } | null;
  proof: { event_id: string; source_recorded: boolean } | null;
  files: Record<string, { size: number; sha256: string }>;
  case_revision_query: string | null;
};
type Revision = { event_id: string; created_at: string; actor_id?: number; provenance?: Record<string, unknown> };
type MeshResults = {
  publication: {stale: boolean};
  artifacts?: {volume_cells: number; mesh_source?: string};
  quality: {
    gate?: {status: string; reason?: string};
    metrics: Record<string, number>;
    quality_preset?: string;
    element_families?: {native_counts: Record<string, number>; geometric_dimension: number};
    refinement?: {axis: string; base_cells: number; selected_cells: number; refined_cells: number};
    boundary_layers?: {plan: {patch: string}; thickness_ranges_m: [number, number][]; maximum_growth: number};
  };
};
type SolveResults = {
  publication: {stale: boolean};
  initial_source?: InitialSource;
  evidence: {
    configuration?: {momentum_convection?: string; declared_linear_solvers?: Record<string, {
      solver: string; preconditioner?: string; smoother?: string; tolerance: number;
    }>};
    adaptive_time_steps?: {samples: number; minimum_s: number; maximum_s: number};
    pimple_outer_iterations?: {observed: number; maximum_index: number};
    linear_convergence?: Record<string, {samples: number; out_of_tolerance: number; maximum_final_residual: number | null}>;
    decomposition?: {method: string; subdomains: number; divisions?: number[]};
    dynamic_refinement?: { changes: { time_s: number; before: number; after: number }[];
      written_cells: Record<string, number> };
    structural_feedback?: { publication_id: string; moving_patch: string; source_time: number; foam_time_s: number;
      matched_points: number; fixed_points: number; maximum_displacement_m: number; maximum_coordinate_error_m: number };
    overset?: {
      interfaces: {name: string; patch: string; interpolation: string}[];
      zone_cells: Record<string, number>;
      total_cells: number;
      calculated_cells: number;
      interpolated_cells: number;
      hole_cells: number;
      motion?: {cell_zone: string; velocity_m_s: number[]; expected_displacement_m: number[];
        translated_points: number; stationary_points: number; time_s: number};
    };
  };
};
type FieldValue = number | number[];
type InitialSource =
  | {mode: "restart"; publication_id: string; generation: number; time: number; fields: Record<string, unknown>}
  | {mode: "mapped"; publication_id: string; generation: number; time: number; fields: Record<string, unknown>;
      mapping: {source_cells: number; covered_target_cells: number; target_cells: number}}
  | {mode: "analytical"; cells: number; formulas: Record<string, {axis: string; origin_m: number;
      value: FieldValue; slope_per_m: FieldValue}>}
  | {mode: "fromMeasurement"; cells: number; samples: number; maximum_observed_distance_m: number;
      measurement: {path: string; maximum_distance_m: number; fields: Record<string, unknown>}};
const meshGateLabels: Record<string, string> = {pass: "通过", warn: "有警告", allowed_with_reason: "按记录理由放行", blocked: "已阻止"};
const meshMetricLabels: Record<string, string> = {non_orthogonality: "最大非正交角（°）", skewness: "最大偏斜度", aspect_ratio: "最大长宽比",
  minimum_volume: "最小体积（m³）", jacobian: "最小雅可比指标"};
const precision = new Intl.NumberFormat("zh-CN", {maximumSignificantDigits: 6});
const scientific = new Intl.NumberFormat("zh-CN", {notation: "scientific", maximumSignificantDigits: 6});
function evidenceNumber(value: number) {
  return (value !== 0 && Math.abs(value) < .001 ? scientific : precision).format(value);
}
const names = new Map([
  ["geometry", "几何"], ["mesh", "网格"], ["solve", "求解"], ["postprocess", "后处理"],
  ["report", "报告"], ["report-source", "报告草稿"], ["report-source-generated", "生成版报告草稿"],
]);

function kindLabel(kind: string) {
  const family = kind.split(":")[0];
  return family === "case-command" ? "命令产物" : names.get(family) ?? kind;
}

function DeclaredEntries({ label, value }: { label: string; value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries = Object.entries(value);
  if (!entries.length) return null;
  return <ul className="case-artifact-paths" aria-label={label}>
    {entries.map(([name, entry]) => {
      const fields = entry && typeof entry === "object" ? entry as Record<string, unknown> : {};
      const description = [fields.title, fields.text, fields.kind, entry].find(item => typeof item === "string");
      return <li key={name}><strong>{name}</strong> · {description ?? JSON.stringify(entry)}</li>;
    })}
  </ul>;
}

export default function CaseArtifactTrace({ caseId }: { caseId: number }) {
  const [open, setOpen] = useState(false);
  const [cursors, setCursors] = useState<string[]>([]);
  const [version, setVersion] = useState(0);
  const [page, setPage] = useState<{ key: string; value?: Page; error?: boolean }>();
  const [path, setPath] = useState<string[]>([]);
  const endpoint = `/cases/${caseId}/provenance/artifacts`;
  const before = cursors.at(-1);
  const key = `${before ?? ""}:${version}`;
  const current = page?.key === key ? page : undefined;
  const next = current?.value?.next_before_id;
  const selected = path.at(-1);

  useEffect(() => {
    if (!open) return;
    let live = true;
    const parameters = new URLSearchParams({ limit: "12" });
    if (before) parameters.set("before_id", before);
    api.get<Page>(`${endpoint}?${parameters}`)
      .then(value => { if (live) setPage({ key, value }); })
      .catch(() => { if (live) setPage({ key, error: true }); });
    return () => { live = false; };
  }, [before, endpoint, key, open]);

  const refresh = () => { setCursors([]); setVersion(value => value + 1); };
  return <details className="case-artifact-trace" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>产物追溯</summary>
    {open && <div className="case-intent-history-body">
      {!current && <div role="status">读取发布列表…</div>}
      {current?.error && <div role="alert">无法读取产物列表。<button type="button" onClick={refresh}>重试</button></div>}
      {current?.value && <>
        {current.value.items.length === 0 ? <div>算例产生已发布的产物后，会在这里列出。</div>
          : <ul className="case-artifact-choices" aria-label="已发布产物">
            {current.value.items.map(item => <li key={item.id}>
              <button type="button" aria-pressed={selected === item.id} onClick={() => setPath([item.id])}>
                {kindLabel(item.kind)} · 作业 #{item.job_id} · 版本 {item.generation}
                <span>{item.active ? "当前指针" : "历史版本"}</span>
              </button>
            </li>)}
          </ul>}
        <nav aria-label="产物分页">
          {cursors.length > 0 && <button type="button" onClick={() => setCursors(values => values.slice(0, -1))}>较新版本</button>}
          {next && <button type="button" onClick={() => setCursors(values => [...values, next])}>较早版本</button>}
          <button type="button" onClick={refresh}>刷新发布</button>
        </nav>
      </>}
      {selected && <TraceDetails key={`${selected}:${version}`} caseId={caseId} endpoint={endpoint} identifier={selected}
        back={path.length > 1 ? () => setPath(values => values.slice(0, -1)) : undefined}
        visit={identifier => setPath(values => [...values, identifier])} />}
    </div>}
  </details>;
}

function MeshEvidence({ caseId, publicationId }: {caseId: number; publicationId: string}) {
  const [result, setResult] = useState<MeshResults>();
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({publication_id: publicationId});
    api.get<MeshResults>(`/cases/${caseId}/mesh/results?${query}`)
      .then(value => { if (live) setResult(value); })
      .catch(() => { if (live) setError(true); });
    return () => { live = false; };
  }, [caseId, publicationId, retry]);
  if (error) return <div role="alert">无法读取该版本的网格证据。 <button type="button" onClick={() => {
    setError(false); setRetry(value => value + 1);
  }}>重试</button></div>;
  if (!result) return <div role="status">读取网格证据…</div>;
  const {gate, metrics, element_families: families, refinement, boundary_layers: layers} = result.quality;
  return <section aria-label="网格质量证据">
    <strong>网格质量证据</strong>
    {result.publication.stale && <p>该版本对应历史输入。</p>}
    <dl>
      {gate && <><dt>质量门禁</dt><dd>{meshGateLabels[gate.status] ?? gate.status}{gate.reason ? ` · ${gate.reason}` : ""}</dd></>}
      {result.quality.quality_preset && <><dt>质量预设</dt><dd>{result.quality.quality_preset}</dd></>}
      {result.artifacts?.mesh_source && <><dt>网格源</dt><dd>{result.artifacts.mesh_source}</dd></>}
      {result.artifacts && <><dt>体单元数</dt><dd>{result.artifacts.volume_cells.toLocaleString("zh-CN")}</dd></>}
      {Object.entries(metrics).map(([name, value]) => <Fragment key={name}>
        <dt>{meshMetricLabels[name] ?? name}</dt><dd>{evidenceNumber(value)}</dd>
      </Fragment>)}
      {families && <><dt>原生单元族</dt><dd>{families.geometric_dimension}D · {Object.entries(families.native_counts)
        .map(([kind, count]) => `${kind} ${count.toLocaleString("zh-CN")}`).join("，")}</dd></>}
      {refinement && <><dt>方向加密</dt><dd>{refinement.axis} 轴 · {refinement.base_cells} → {refinement.refined_cells} 单元 · 选中 {refinement.selected_cells}</dd></>}
      {layers && <><dt>边界层</dt><dd>{layers.plan.patch} · {layers.thickness_ranges_m.length} 层 · 最大增长率 {evidenceNumber(layers.maximum_growth)}</dd>
        <dt>法向厚度（m）</dt><dd>{layers.thickness_ranges_m.map(([low, high]) => low === high ? evidenceNumber(low)
          : `${evidenceNumber(low)}–${evidenceNumber(high)}`).join("，")}</dd></>}
    </dl>
  </section>;
}

const initialSourceLabels = {restart: "同网格历史场", mapped: "异网格映射", analytical: "解析公式", fromMeasurement: "CSV 采样"};
const fieldNumber = (value: FieldValue) => Array.isArray(value) ? `(${value.map(evidenceNumber).join(", ")})` : evidenceNumber(value);

function InitialFieldEvidence({source, visit}: {source: InitialSource; visit: (id: string) => void}) {
  const label = initialSourceLabels[source.mode];
  if (!label) return null;
  return <section aria-label="初值来源" className="case-initial-evidence">
    <strong>初值来源</strong>
    <dl>
      <dt>方式</dt><dd>{label}</dd>
      {(source.mode === "restart" || source.mode === "mapped") && <>
        <dt>源成果</dt><dd><button type="button" onClick={() => visit(source.publication_id)}>
          查看求解版本 {source.generation}</button></dd>
        <dt>源时刻</dt><dd>{evidenceNumber(source.time)} s</dd>
        <dt>初值字段</dt><dd>{Object.keys(source.fields).join("、")}</dd>
      </>}
      {source.mode === "mapped" && <>
        <dt>源网格</dt><dd>{source.mapping.source_cells.toLocaleString("zh-CN")} 单元</dd>
        <dt>目标覆盖</dt><dd>{source.mapping.covered_target_cells.toLocaleString("zh-CN")} / {source.mapping.target_cells.toLocaleString("zh-CN")} 单元</dd>
      </>}
      {source.mode === "analytical" && <>
        <dt>目标网格</dt><dd>{source.cells.toLocaleString("zh-CN")} 单元</dd>
        {Object.entries(source.formulas).map(([field, formula]) => <Fragment key={field}>
          <dt>{field} 线性初值（SI）</dt><dd>{formula.axis} 轴 · 原点 {evidenceNumber(formula.origin_m)} m · 基准 {fieldNumber(formula.value)} · 每米变化 {fieldNumber(formula.slope_per_m)}</dd>
        </Fragment>)}
      </>}
      {source.mode === "fromMeasurement" && <>
        <dt>来源文件</dt><dd><code>{source.measurement.path.match(/[^/]+\/?/g)?.map((part, index) =>
          <Fragment key={index}>{part}<wbr /></Fragment>)}</code></dd>
        <dt>采样覆盖</dt><dd>{source.samples.toLocaleString("zh-CN")} 个样本 · {source.cells.toLocaleString("zh-CN")} 个单元</dd>
        <dt>最近点距离</dt><dd>最大 {evidenceNumber(source.maximum_observed_distance_m)} m · 上限 {evidenceNumber(source.measurement.maximum_distance_m)} m</dd>
        <dt>初值字段</dt><dd>{Object.keys(source.measurement.fields).join("、")}</dd>
      </>}
    </dl>
  </section>;
}

function NumericalEvidence({caseId, publicationId, visit}: {caseId: number; publicationId: string; visit: (id: string) => void}) {
  const [result, setResult] = useState<SolveResults>();
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({publication_id: publicationId});
    api.get<SolveResults>(`/cases/${caseId}/solve/results?${query}`)
      .then(value => { if (live) setResult(value); })
      .catch(() => { if (live) setError(true); });
    return () => { live = false; };
  }, [caseId, publicationId, retry]);
  if (error) return <div role="alert">无法读取该版本的求解证据。 <button type="button" onClick={() => {
    setError(false); setRetry(value => value + 1);
  }}>重试</button></div>;
  if (!result) return <div role="status">读取求解证据…</div>;
  const {configuration, adaptive_time_steps: steps, pimple_outer_iterations: iterations,
    linear_convergence: convergence, decomposition, overset, dynamic_refinement: refinement,
    structural_feedback: feedback} = result.evidence;
  const solvers = Object.entries(configuration?.declared_linear_solvers ?? {});
  const hasNumericalEvidence = solvers.length > 0 || !!(configuration?.momentum_convection || steps || iterations || decomposition);
  return <>
    {result.initial_source && <InitialFieldEvidence source={result.initial_source} visit={visit} />}
    {refinement && <section aria-label="相界面动态加密" className="case-solve-evidence">
      <strong>相界面动态加密</strong>
      <dl>
        <dt>活动单元</dt><dd>{refinement.changes[0].before} → {refinement.changes.at(-1)?.after}</dd>
        <dt>首次网格变化</dt><dd>{evidenceNumber(refinement.changes[0].time_s)} s</dd>
        <dt>实际拓扑变化</dt><dd>{refinement.changes.length} 次</dd>
        <dt>已核验网格时刻</dt><dd>{Object.keys(refinement.written_cells).length} 个</dd>
      </dl>
    </section>}
    {feedback && <section aria-label="结构位移反馈" className="case-solve-evidence">
      <strong>结构位移反馈</strong>
      <dl>
        <dt>移动边界</dt><dd>{feedback.moving_patch} · {feedback.matched_points} 个匹配点</dd>
        <dt>固定点</dt><dd>{feedback.fixed_points} 个</dd>
        <dt>最大位移</dt><dd>{evidenceNumber(feedback.maximum_displacement_m)} m</dd>
        <dt>OpenFOAM 写出时间</dt><dd>{evidenceNumber(feedback.foam_time_s)} s</dd>
        <dt>CalculiX 发布</dt><dd>{feedback.publication_id}</dd>
      </dl>
    </section>}
    {overset && <section aria-label="Overset 插值证据" className="case-solve-evidence">
      <strong>Overset 插值证据</strong>
      <dl>
        <dt>插值边界</dt><dd>{overset.interfaces.map(item => item.patch).join("、")}</dd>
        <dt>插值方法</dt><dd>{overset.interfaces[0]?.interpolation}</dd>
        <dt>区域单元数</dt><dd>{Object.entries(overset.zone_cells).map(([zone, cells]) =>
          `区域 ${zone} · ${cells.toLocaleString("zh-CN")}`).join("、")}</dd>
        <dt>实际单元分类</dt><dd className="case-overset-counts">
          <span>共 {overset.total_cells}</span><span>求解 {overset.calculated_cells}</span>
          <span>插值 {overset.interpolated_cells}</span><span>孔洞 {overset.hole_cells}</span>
        </dd>
        {overset.motion && <>
          <dt>运动网格区</dt><dd>{overset.motion.cell_zone} · ({overset.motion.velocity_m_s.map(evidenceNumber).join(", ")}) m/s</dd>
          <dt>{evidenceNumber(overset.motion.time_s)} s 位移</dt>
          <dd>({overset.motion.expected_displacement_m.map(evidenceNumber).join(", ")}) m</dd>
          <dt>实际网格点</dt><dd>平移 {overset.motion.translated_points} · 静止 {overset.motion.stationary_points}</dd>
        </>}
      </dl>
    </section>}
    {hasNumericalEvidence && <section aria-label="数值方法证据">
    <strong>数值方法证据</strong>
    {result.publication.stale && <p>该版本对应历史输入。</p>}
    <dl>
      {solvers.map(([field, settings]) => <Fragment key={field}>
        <dt>{field} 线性求解器</dt><dd>{settings.solver} · {settings.preconditioner ?? settings.smoother}
          {` · 绝对容差 ${evidenceNumber(settings.tolerance)}`}</dd>
      </Fragment>)}
      {Object.entries(convergence ?? {}).map(([field, observation]) => <Fragment key={field}>
        <dt>{field} 实际残差</dt><dd>{observation.samples ? `${observation.samples} 次求解，${observation.out_of_tolerance} 次超容差` : "无残差记录"}
          {observation.maximum_final_residual !== null && ` · 最大最终残差 ${evidenceNumber(observation.maximum_final_residual)}`}</dd>
      </Fragment>)}
      {configuration?.momentum_convection && <><dt>动量对流格式</dt><dd>{configuration.momentum_convection}</dd></>}
      {steps && <><dt>实际时间步（s）</dt><dd>{evidenceNumber(steps.minimum_s)}–{evidenceNumber(steps.maximum_s)} · {steps.samples} 个步长</dd></>}
      {iterations && <><dt>PIMPLE 外迭代</dt><dd>{iterations.observed} 次 · 最大编号 {iterations.maximum_index}</dd></>}
      {decomposition && <><dt>实际 MPI 分区</dt><dd>{decomposition.method} · {decomposition.subdomains} 个分区
        {decomposition.divisions && ` · ${decomposition.divisions.join(" × ")}`}</dd></>}
    </dl>
    </section>}
  </>;
}

function TraceDetails({ caseId, endpoint, identifier, back, visit }: {
  caseId: number; endpoint: string; identifier: string; back?: () => void; visit: (identifier: string) => void;
}) {
  const [trace, setTrace] = useState<Trace>();
  const [error, setError] = useState(false);
  const [savedRevision, setRevision] = useState<{ value?: Revision; error?: string }>();
  const [readingRevision, setReadingRevision] = useState(false);
  const [fileLimit, setFileLimit] = useState(12);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let live = true;
    api.get<Trace>(`${endpoint}/${encodeURIComponent(identifier)}`)
      .then(value => { if (live) setTrace(value); })
      .catch(() => { if (live) setError(true); });
    return () => { live = false; };
  }, [endpoint, identifier, retry]);

  async function readRevision(query: string) {
    setReadingRevision(true);
    try {
      const record = await api.get<{ matched_revision: Revision | null; chain_valid: boolean }>(query);
      setRevision(record.chain_valid && record.matched_revision ? { value: record.matched_revision }
        : { error: record.chain_valid ? "历史中没有与该封存摘要匹配的修订。" : "修订链校验失败。" });
    } catch {
      setRevision({ error: "读取修订失败，请重试。" });
    } finally {
      setReadingRevision(false);
    }
  }

  if (error) return <div role="alert">无法核验所选版本。
    <button type="button" onClick={() => { setError(false); setRetry(value => value + 1); }}>重试</button>
    {back && <button type="button" onClick={back}>返回上一产物</button>}
  </div>;
  if (!trace) return <div role="status">读取所选版本…</div>;
  const dictionary = trace.source?.case_dict;
  const revisionQuery = trace.case_revision_query;
  const paths = Object.keys(trace.files);
  const revision = savedRevision?.value;

  return <section className="case-artifact-details" aria-label="产物来源">
    {back && <button type="button" onClick={back}>返回上一产物</button>}
    <strong>{kindLabel(trace.publication.kind)} · 版本 {trace.publication.generation}</strong>
    <dl>
      <dt>发布摘要</dt><dd><code>{trace.publication.manifest_sha256}</code></dd>
      {dictionary?.revision !== undefined && <><dt>caseDict 修订</dt><dd>{dictionary.revision}</dd></>}
      {dictionary?.sha256 && <><dt>caseDict 摘要</dt><dd><code>{dictionary.sha256}</code></dd></>}
      {trace.proof && <><dt>{trace.proof.source_recorded ? "发布回执" : "历史发布回执"}</dt>
        <dd><code>{trace.proof.event_id}</code></dd></>}
    </dl>
    {revisionQuery && <div>
      <button type="button" disabled={readingRevision}
        onClick={() => void readRevision(revisionQuery)}>{readingRevision ? "核对中…" : "核对算例修订"}</button>
      {savedRevision?.error && <div role="alert">{savedRevision.error}</div>}
      {revision && <div role="status">
        已匹配修订 <code>{revision.event_id}</code>
        {revision.actor_id !== undefined && <div>修订者 #{revision.actor_id}</div>}
        <time dateTime={revision.created_at}>{new Date(revision.created_at).toLocaleString()}</time>
        <DeclaredEntries label="修订来源" value={revision.provenance?.sources} />
        <DeclaredEntries label="修订假设" value={revision.provenance?.assumptions} />
        <DeclaredEntries label="修订决策" value={revision.provenance?.decisions} />
      </div>}
    </div>}
    {!!trace.source?.input_publications.length && <div className="case-artifact-upstream" aria-label="上游发布">
      <span>上游发布</span>
      {trace.source.input_publications.map(item => <button type="button" key={item.publication_id}
        onClick={() => visit(item.publication_id)}>{kindLabel(item.kind)} · 版本 {item.generation}</button>)}
    </div>}
    {trace.publication.kind === "mesh" && <MeshEvidence caseId={caseId} publicationId={identifier} />}
    {trace.publication.kind === "solve" && <NumericalEvidence caseId={caseId} publicationId={identifier} visit={visit} />}
    <details>
      <summary>{paths.length} 个已登记文件</summary>
      <ul className="case-artifact-paths">{paths.slice(0, fileLimit).map(name => <li key={name}>{name}</li>)}</ul>
      {fileLimit < paths.length && <button type="button" onClick={() => setFileLimit(value => value + 12)}>显示更多文件</button>}
    </details>
  </section>;
}
