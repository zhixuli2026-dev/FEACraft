import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useCaseDraft } from "../utils/useCaseDraft";
import type { CaseDictSnapshot, DictReceipt } from "../utils/useCaseDraft";
import { DraftReview, draftError } from "./CaseDraftFeedback";
import "./CaseMetaEditor.css";

type MetadataSchema = {
  enums: Record<string, string[]>;
  text_fields: string[];
  units: Record<string, string[]>;
  time_behaviors: string[];
};
type Issue = {path: string; message: string; severity: "error" | "warning"};
type FormProps = {caseId: number; snapshot: CaseDictSnapshot; onSaved: () => Promise<void>};
const labels: Record<string, string> = {
  status: "状态", discipline: "学科", analysisType: "分析类型", dimensions: "维度",
  fidelity: "精度层级", executionProfile: "执行方式", caseName: "算例名称", displayName: "显示名称",
  description: "说明", owner: "记录的负责人", createdBy: "记录的创建人", createdAt: "创建时间",
  updatedAt: "更新时间", project: "项目", revision: "项目修订", notes: "备注",
  length: "长度", mass: "质量", time: "时间", temperature: "温度", angle: "角度",
  force: "力", pressure: "压力", energy: "能量",
};

function stringFields(values: unknown) {
  if (values === null || typeof values !== "object" || Array.isArray(values)) return {};
  return Object.fromEntries(Object.entries(values).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
}

function readDraft(snapshot: CaseDictSnapshot) {
  const meta = snapshot.parsed?.caseMeta ?? {};
  const timeBehavior = snapshot.parsed?.physicalProblem?.timeBehavior;
  const tags: unknown[] = Array.isArray(meta.tags) ? meta.tags : [];
  return {
    fields: stringFields(meta),
    units: stringFields(meta.units),
    tags: tags.filter(tag => typeof tag === "string").join("\n"),
    timeBehavior: typeof timeBehavior === "string" ? timeBehavior : "",
  };
}

function emptyEdits() {
  return {fields: new Set<string>(), units: new Set<string>(), tags: false, timeBehavior: false};
}

function buildPatch(next: ReturnType<typeof readDraft>, edited: ReturnType<typeof emptyEdits>) {
  const changes: Record<string, unknown> = Object.fromEntries([...edited.fields].map(key => [key, next.fields[key] || null]));
  if (edited.units.size) changes.units = Object.fromEntries([...edited.units].map(key => [key, next.units[key] || null]));
  if (edited.tags) changes.tags = next.tags.split("\n").filter(line => line.trim());
  return {changes, ...(edited.timeBehavior ? {time_behavior: next.timeBehavior || null} : {})};
}

function rebaseDraft(remote: ReturnType<typeof readDraft>, local: ReturnType<typeof readDraft>, edited: ReturnType<typeof emptyEdits>) {
  const fields = {...remote.fields, ...Object.fromEntries([...edited.fields].map(key => [key, local.fields[key]]))};
  const units = {...remote.units, ...Object.fromEntries([...edited.units].map(key => [key, local.units[key]]))};
  return {fields, units, tags: edited.tags ? local.tags : remote.tags,
    timeBehavior: edited.timeBehavior ? local.timeBehavior : remote.timeBehavior};
}

function OptionField({label, values, current, onChange}: {
  label: string; values: string[]; current: string; onChange: (value: string) => void;
}) {
  return <label>{label}
    <select aria-label={label} value={current} onChange={event => onChange(event.target.value)}>
      <option value="">未设置</option>
      {current && !values.includes(current) && <option value={current}>{current}</option>}
      {values.map(value => <option key={value} value={value}>{value}</option>)}
    </select>
  </label>;
}

export default function CaseMetaEditor({caseId, snapshot, onSaved}: FormProps) {
  const [schema, setSchema] = useState<MetadataSchema>();
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [issues, setIssues] = useState<Issue[]>([]);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const edited = useRef(emptyEdits());
  const route = `/cases/${caseId}/caseDict/meta`;
  const source = readDraft(snapshot);
  const form = useCaseDraft(snapshot, source, "caseMeta");
  const draft = form.draft.value;

  useEffect(() => {
    let active = true;
    api.get<MetadataSchema>(`${route}-schema`).then(value => {if (active) setSchema(value);})
      .catch(cause => {if (active) setError(draftError(cause));});
    return () => {active = false;};
  }, [route]);

  const edit = (next: typeof draft) => {form.edit(next); setIssues([]); setFeedback("");};
  const editField = (group: "fields" | "units", key: string, value: string) => {
    edited.current[group].add(key);
    edit({...draft, [group]: {...draft[group], [key]: value}});
  };
  const update = async (lint: boolean) => {
    if (busyRef.current || !schema) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setFeedback("");
    setIssues([]);
    try {
      const submitted = form.capture();
      const body = {...submitted.conditions, ...buildPatch(submitted.value, edited.current)};
      if (lint) {
        const result = await api.post<{valid: boolean; issues: Issue[]}>(`${route}:lint`, body);
        setIssues(result.issues);
        setFeedback(result.valid ? "校验通过" : "");
      } else {
        const receipt = await api.patch<DictReceipt>(route, body);
        form.accept(submitted, receipt);
        edited.current = emptyEdits();
        await onSaved();
        setFeedback("已保存算例元数据");
      }
    } catch (cause) {
      setError(draftError(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return <details className="case-meta-editor" aria-label="算例元数据">
    <summary>算例元数据</summary>
    {error && <div role="alert">
      <p>{error}</p>
      {schema && <button type="button" disabled={busy} onClick={async () => {
        try { await form.inspect(caseId); } catch (cause) { setError(draftError(cause)); }
      }}>查看服务器版本</button>}
    </div>}
    {form.review && <DraftReview value={{caseMeta: form.review.parsed?.caseMeta,
      timeBehavior: form.review.parsed?.physicalProblem?.timeBehavior}} onChoose={keep => {
        if (form.review) {
          const remote = readDraft(form.review);
          form.resolve(remote, keep);
          if (keep) form.edit(rebaseDraft(remote, draft, edited.current));
        }
        if (!keep) edited.current = emptyEdits();
        setError("");
        void onSaved();
      }} />}
    {!schema && !error && <p role="status">正在读取元数据选项…</p>}
    {schema && <form data-testid="case-meta-form" data-dirty={form.draft.dirty}
      onSubmit={event => {event.preventDefault(); void update(false);}}>
      <fieldset disabled={busy}>
        <div className="case-meta-fields">
          {Object.entries(schema.enums).map(([key, values]) => <OptionField key={key}
            label={labels[key] ?? key} values={values} current={draft.fields[key] ?? ""}
            onChange={value => editField("fields", key, value)} />)}
          <OptionField label="时间行为" values={schema.time_behaviors} current={draft.timeBehavior}
            onChange={timeBehavior => {edited.current.timeBehavior = true; edit({...draft, timeBehavior});}} />
        </div>
        <details>
          <summary>名称、项目和标签</summary>
          <div className="case-meta-fields">
            {schema.text_fields.map(key => <label key={key}>{labels[key] ?? key}
              <input value={draft.fields[key] ?? ""}
                onChange={event => editField("fields", key, event.target.value)} />
            </label>)}
          </div>
          <label>标签（每行一个）
            <textarea aria-label="标签（每行一个）" rows={3} value={draft.tags}
              onChange={event => {edited.current.tags = true; edit({...draft, tags: event.target.value});}} />
          </label>
        </details>
        <details>
          <summary>单位</summary>
          <div className="case-meta-fields">
            {Object.entries(schema.units).map(([unit, values]) => <OptionField key={unit}
              label={labels[unit] ?? unit} values={values} current={draft.units[unit] ?? ""}
              onChange={value => editField("units", unit, value)} />)}
          </div>
        </details>
        <div className="case-meta-actions">
          <button type="button" onClick={() => void update(true)}>检查配置</button>
          <button type="submit" disabled={!form.draft.dirty}>保存元数据</button>
        </div>
      </fieldset>
      {feedback && <p role="status">{feedback}</p>}
      {issues.length > 0 && <ul aria-label="配置检查结果" aria-live="polite">
        {issues.map((issue, index) => <li key={`${issue.path}-${index}`}>
          {issue.severity === "warning" ? "提示" : "错误"} · {issue.path} · {issue.message}
        </li>)}
      </ul>}
    </form>}
  </details>;
}
