import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import { activatePanel } from "../shell/dock";
import { useStore } from "../store";
import { useCaseDraft } from "../utils/useCaseDraft";
import type { CaseDictSnapshot, DictReceipt } from "../utils/useCaseDraft";
import { DraftReview, draftError } from "./CaseDraftFeedback";
import "./CaseErrors.css";

type ErrorEntry = {
  title?: string;
  stage?: string;
  category?: string;
  severity?: string;
  status?: string;
  blocking?: boolean | string;
  message?: string;
  relatedPaths?: string[] | string;
  fixOptions?: Record<string, unknown>;
  resolution?: unknown;
  verification?: unknown;
};

type CaseErrorsProps = {caseId: number; snapshot: CaseDictSnapshot; onSaved: () => Promise<void>};

const stages = {elicitation: "需求", geometry: "几何", mesh: "网格", run: "求解", post: "后处理", report: "报告", toolchain: "工具链"};
const categories = {missingInput: "缺少输入", geometry: "几何", meshQuality: "网格质量", conversion: "转换", solver: "求解器",
  postprocess: "后处理", report: "报告", environment: "运行环境"};
const severities = {info: "提示", warning: "警告", error: "错误", fatal: "严重错误"};
const statuses: Record<string, string> = {open: "未处理", investigating: "处理中", fixProposed: "已提出修复",
  fixed: "已修复", verified: "已验证", wontFix: "不予修复"};
const actions = [
  ["investigate", "investigating", "开始处理"],
  ["propose", "fixProposed", "提出修复"],
  ["fixed", "fixed", "标记修复"],
  ["verified", "verified", "验证结清"],
  ["wontfix", "wontFix", "不予修复"],
  ["reopen", "open", "重开"],
];

function displayText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null || Array.isArray(value) && !value.length) return "";
  return JSON.stringify(value) ?? "";
}

function evidencePaths(value: ErrorEntry["relatedPaths"]): string[] {
  if (typeof value === "string") return value ? [value] : [];
  return value ?? [];
}

function editableFields(entry: ErrorEntry = {}, id = "") {
  return {
    id,
    title: entry.title ?? "",
    stage: entry.stage ?? "elicitation",
    category: entry.category ?? "missingInput",
    severity: entry.severity ?? "error",
    blocking: String(entry.blocking ?? false).toLowerCase() === "true",
    message: entry.message ?? "",
    relatedPaths: evidencePaths(entry.relatedPaths),
    fixOptions: displayText(entry.fixOptions?.text),
    resolution: displayText(entry.resolution),
    verification: displayText(entry.verification),
  };
}

function FieldSelect({label, value, choices, onChange}: {
  label: string; value: string; choices: Record<string, string>; onChange: (value: string) => void;
}) {
  return <label>{label}
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value)}>
      {Object.entries(choices).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
    </select>
  </label>;
}

function EvidenceReferences({paths, onChange, onError}: {
  paths: string[]; onChange: (paths: string[]) => void; onError: (message: string) => void;
}) {
  const id = useId();
  const [path, setPath] = useState("");
  const tree = useStore(state => state.fileTree);
  const candidate = path.trim();
  const available = tree.filter(item => item.kind !== "directory" && !paths.includes(item.path)
    && item.path.toLowerCase().includes(candidate.toLowerCase())).slice(0, 30);
  const open = async (reference: string) => {
    try {
      if (await useStore.getState().openFile(reference)) activatePanel("editor");
    } catch (error) {
      onError(`证据文件打开失败：${draftError(error)}`);
    }
  };
  return <div>
    <div className="case-errors-reference-input">
      <label>相关证据文件
        <input value={path} onChange={event => setPath(event.target.value)}
          list={id} placeholder="算例内的文件路径" />
      </label>
      <datalist id={id}>{available.map(item => <option key={item.path} value={item.path} />)}</datalist>
      <button type="button" disabled={!candidate || paths.includes(candidate)} onClick={() => {
        onChange([...paths, candidate]);
        setPath("");
      }}>添加证据</button>
    </div>
    {paths.length > 0 && <ul className="case-errors-references">
      {paths.map(reference => <li key={reference}>
        <button type="button" onClick={() => void open(reference)}>{reference}</button>
        <button type="button" aria-label={`移除证据 ${reference}`}
          onClick={() => onChange(paths.filter(value => value !== reference))}>移除</button>
      </li>)}
    </ul>}
  </div>;
}

function ErrorEntryEditor({caseId, snapshot, onSaved, id, entry, present = true, onDirtyChange, close}: CaseErrorsProps & {
  id: string;
  entry?: ErrorEntry;
  present?: boolean;
  onDirtyChange?: (id: string, entry: ErrorEntry, dirty: boolean) => void;
  close?: () => void;
}) {
  const creating = !entry;
  const status = entry?.status ?? "open";
  const initial = editableFields(entry, id);
  const form = useCaseDraft(snapshot, initial, id);
  const draft = form.draft.value;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const busyRef = useRef(false);
  useEffect(() => {
    if (entry) onDirtyChange?.(id, entry, form.draft.dirty);
  }, [id, entry, form.draft.dirty]);
  const setField = <K extends keyof typeof draft,>(key: K, value: (typeof draft)[K]) => form.edit({...draft, [key]: value});

  const save = async (newStatus: string) => {
    if (busyRef.current || !present) return;
    if (!draft.title.trim() || creating && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test(draft.id)) {
      setError(!draft.title.trim() ? "请输入错误标题。" : "编号需使用字母或数字开头，最多 80 个字符，可包含点、下划线或连字符。");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const submitted = form.capture();
      const value = submitted.value;
      const body = {
        ...submitted.conditions, status: newStatus,
        title: value.title, stage: value.stage, category: value.category, severity: value.severity,
        message: value.message, blocking: value.blocking,
        ...(JSON.stringify(value.relatedPaths) !== JSON.stringify(initial.relatedPaths) ? {relatedPaths: value.relatedPaths} : {}),
        ...(value.fixOptions !== initial.fixOptions ? {fixOptions: {...entry?.fixOptions, text: value.fixOptions}} : {}),
        ...(value.resolution !== initial.resolution ? {resolution: value.resolution} : {}),
        ...(value.verification !== initial.verification ? {verification: value.verification} : {}),
      };
      const route = `/cases/${caseId}/caseDict/errors`;
      const receipt = creating ? await api.post<DictReceipt>(route, {...body, id: value.id})
        : await api.patch<DictReceipt>(`${route}/${encodeURIComponent(id)}`, body);
      form.accept(submitted, receipt);
      await onSaved();
      if (creating) close?.();
    } catch (cause) {
      setError(draftError(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const review = form.review?.parsed?.errors?.entries?.[creating ? draft.id : id];
  return <form className="case-errors-entry" data-testid={creating ? "error-create-form" : `error-entry-${id}`}
    data-status={status} data-dirty={form.draft.dirty} onSubmit={event => {event.preventDefault(); void save(status);}}>
    {!creating && <div className="case-errors-entry-heading">
      <strong>{entry.title} · {id}</strong>
      <span className="case-errors-muted">
        {statuses[status]} <span hidden data-testid={`error-status-${id}`}>{status}</span>
        {initial.blocking && " · 阻断阶段推进"}
      </span>
    </div>}
    {(error || !present) && <div role="alert" className="case-errors-alert">
      <div>{error || "服务器已移除此条目，草稿已保留。请查看服务器版本后决定是否放弃草稿。"}</div>
      <button type="button" disabled={busy || inspecting} onClick={async () => {
        setInspecting(true);
        try { await form.inspect(caseId); } catch (cause) { setError(draftError(cause)); }
        finally { setInspecting(false); }
      }}>{inspecting ? "读取中…" : "查看服务器版本"}</button>
    </div>}
    {form.review && <DraftReview value={review} allowKeep={creating ? !review : !!review} onChoose={keep => {
      form.resolve(editableFields(review, id), keep);
      setError("");
      void onSaved();
    }} />}
    <fieldset disabled={busy || !present}>
      <div className="case-errors-fields">
        {creating && <label>错误编号<input required value={draft.id} onChange={event => setField("id", event.target.value)} /></label>}
        <label>标题<input required value={draft.title} onChange={event => setField("title", event.target.value)} /></label>
        <FieldSelect label="阶段" choices={stages} value={draft.stage} onChange={value => setField("stage", value)} />
        <FieldSelect label="分类" choices={categories} value={draft.category} onChange={value => setField("category", value)} />
        <FieldSelect label="严重程度" choices={severities} value={draft.severity} onChange={value => setField("severity", value)} />
      </div>
      <label className="case-errors-checkbox">
        <input type="checkbox" checked={draft.blocking} onChange={event => setField("blocking", event.target.checked)} /> 阻断阶段推进
      </label>
      <label>错误详情<textarea aria-label="错误详情" rows={2} value={draft.message}
        onChange={event => setField("message", event.target.value)} /></label>
      <EvidenceReferences paths={draft.relatedPaths} onChange={value => setField("relatedPaths", value)} onError={setError} />
      {!creating && <div className="case-errors-fields">
        <label>修复方案<textarea aria-label="修复方案" data-testid={`error-fix-${id}`} rows={2} value={draft.fixOptions}
          onChange={event => setField("fixOptions", event.target.value)} /></label>
        <label>修复内容<textarea aria-label="修复内容" data-testid={`error-resolution-${id}`} rows={2} value={draft.resolution}
          onChange={event => setField("resolution", event.target.value)} /></label>
        <label>验证说明<textarea aria-label="验证说明" data-testid={`error-verification-${id}`} rows={2} value={draft.verification}
          onChange={event => setField("verification", event.target.value)} /></label>
      </div>}
      <div className="case-errors-actions">
        <button type="submit" disabled={!form.draft.dirty}>{creating ? "创建错误" : "保存错误字段"}</button>
        {creating ? <button type="button" onClick={close}>取消</button>
          : actions.filter(([key]) => key === "investigate" ? status === "open" : key !== "reopen" || status !== "open").map(([key, next, label]) =>
            <button key={key} type="button" data-testid={`error-${key}-${id}`} onClick={() => void save(next)}>{label}</button>)}
      </div>
    </fieldset>
  </form>;
}

export default function CaseErrors(props: CaseErrorsProps) {
  const errors = props.snapshot.parsed?.errors ?? {};
  const entries = (errors.entries ?? {}) as Record<string, ErrorEntry>;
  const summary = errors.summary;
  const [adding, setAdding] = useState(false);
  const [retained, setRetained] = useState<Record<string, ErrorEntry>>({});
  const remember = (id: string, entry: ErrorEntry, dirty: boolean) => setRetained(previous => {
    if (dirty && previous[id] === entry || !dirty && !Object.hasOwn(previous, id)) return previous;
    const next = {...previous};
    if (dirty) next[id] = entry; else delete next[id];
    return next;
  });
  return <section className="case-errors" aria-label="算例错误" data-testid="error-lifecycle">
    <header>
      <h3>算例错误</h3>
      <button type="button" onClick={() => setAdding(true)} disabled={adding}>新增错误</button>
    </header>
    {summary && <p className="case-errors-muted" data-testid="error-summary">
      未结 {summary.open ?? 0} · 阻断 {summary.blocking ?? 0} · 已处理 {summary.resolved ?? 0}
    </p>}
    {adding && <ErrorEntryEditor {...props} id="" close={() => setAdding(false)} />}
    {Object.entries({...retained, ...entries}).map(([id, entry]) => <ErrorEntryEditor {...props} key={id} id={id}
      entry={entry} present={Object.hasOwn(entries, id)} onDirtyChange={remember} />)}
  </section>;
}
