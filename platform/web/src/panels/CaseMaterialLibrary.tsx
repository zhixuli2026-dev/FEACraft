import {useId, useRef, useState} from "react";
import {api, ApiError} from "../api";
import type {CaseDictSnapshot} from "../utils/useCaseDraft";
import "./CaseMetaEditor.css";
import "./CaseMaterialLibrary.css";

type Material = Record<string, unknown>;
type Entry = {key: string; revision: number; archived: boolean; material?: Material};
type Catalog = {items: Entry[]; next_after: string | null; schema: {
  enums: Record<string, string[]>; units: Record<string, string[]>;
}};
type Props = {workspaceId: number; caseId: number; snapshot: CaseDictSnapshot; readOnly: boolean; onApplied: () => Promise<void>};

const fieldNames: Record<string, string> = {
  role: "用途", phase: "相态", source: "来源", density: "密度", viscosity: "动力黏度",
  heatCapacity: "比热容", thermalConductivity: "导热率", elasticModulus: "弹性模量",
  poissonRatio: "泊松比", yieldStress: "屈服应力", electricConductivity: "电导率",
  relativePermittivity: "相对介电常数", magneticPermeability: "磁导率",
};
const optionNames: Record<string, string> = {
  fluid: "流体", solid: "固体", porous: "多孔介质", conductor: "导体", dielectric: "电介质", acousticMedium: "声学介质",
  gas: "气相", liquid: "液相", plasma: "等离子体", mixture: "混合物", user: "用户", paper: "论文",
  database: "数据库", tutorial: "教程", assumption: "假设",
};

function quantity(material: Material, field: string, units: string[]) {
  const value = material[field];
  if (value && typeof value === "object" && "value" in value && "unit" in value) {
    return {value: String(value.value), unit: String(value.unit)};
  }
  return {value: value == null ? "" : String(value), unit: units[0]};
}

export default function CaseMaterialLibrary({workspaceId, caseId, snapshot, readOnly, onApplied}: Props) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [head, setHead] = useState<Entry | null>(null);
  const [selected, setSelected] = useState<Entry | null>(null);
  const [material, setMaterial] = useState<Material>({});
  const [key, setKey] = useState("");
  const [version, setVersion] = useState("");
  const [caseName, setCaseName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<Record<string, string>>({});
  const alert = useRef<HTMLDivElement>(null);
  const id = useId();
  const path = `/workspaces/${workspaceId}/material-library`;
  const historical = !!head && !!selected && selected.revision !== head.revision && !head.archived;
  const dirty = JSON.stringify(material) !== JSON.stringify(selected?.material ?? {});
  const archiveReceipt = !!head?.archived && !selected?.material;
  const readonlyFields = readOnly || historical || archiveReceipt;
  const slug = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;

  async function fetchPage(after = "") {
    const page = await api.get<Catalog>(`${path}?include_archived=true&after=${encodeURIComponent(after)}`);
    setCatalog(previous => after && previous ? {...page, items:[...previous.items, ...page.items]} : page);
  }

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setIssues({});
    setMessage("");
    try {
      await action();
    } catch (problem) {
      const detail = problem instanceof ApiError && typeof problem.detail === "string" ? problem.detail : "";
      const invalid = Array.from(detail.matchAll(/materials\.entries\.[^.\s]+\.([A-Za-z]+) (.*?)(?=，materials\.entries\.|$)/g));
      setIssues(Object.fromEntries(invalid.map(match => [match[1], match[2]])));
      setError(problem instanceof Error ? (detail || problem.message) : String(problem));
      requestAnimationFrame(() => alert.current?.focus());
    } finally {
      setBusy(false);
    }
  }

  function choose(entry: Entry | null, revision = entry) {
    setHead(entry);
    setSelected(revision);
    setMaterial(revision?.material ?? {});
    setKey(entry?.key ?? "");
    setVersion(revision ? String(revision.revision) : "");
    setCaseName(entry?.key ?? "");
  }

  async function loadEntry(entryKey: string) {
    if (!entryKey) { choose(null); return; }
    const entry = await api.get<Entry>(`${path}/${entryKey}`);
    const latestMaterial = entry.archived ? await api.get<Entry>(`${path}/${entryKey}?revision=${entry.revision-1}`) : entry;
    choose(entry, latestMaterial);
  }

  function change(field: string, value: unknown) {
    setMaterial(previous => {
      const next = {...previous};
      if (value === undefined || value === "") delete next[field];
      else next[field] = value;
      return next;
    });
    setIssues(previous => {
      const next = {...previous};
      delete next[field];
      return next;
    });
  }

  async function save() {
    const saved = await api.put<Entry>(`${path}/${key}`, {expected_revision:head?.revision ?? 0, material});
    choose(saved);
    await fetchPage();
    setMessage(`已保存版本 ${saved.revision}`);
  }

  async function archive() {
    if (!head) return;
    const archived = await api.post<Entry>(`${path}/${head.key}:archive`, {expected_revision:head.revision});
    setHead(archived);
    await fetchPage();
    setMessage(`已归档 ${head.key}`);
  }

  async function applySelected() {
    if (!selected) return;
    await api.post(`/cases/${caseId}/caseDict/materials:apply`, {
      key:selected.key, library_revision:selected.revision, name:caseName,
      expected_revision:snapshot.dict_revision, expected_sha256:snapshot.content_sha256,
    });
    await onApplied();
    setMessage(`已将 ${selected.key} 版本 ${selected.revision} 应用为 ${caseName}`);
  }

  return <details className="case-meta-editor case-material-library" onToggle={event => {
    if (event.currentTarget.open && !catalog && !busy) void perform(() => fetchPage());
  }}>
    <summary>工作区材料库</summary>
    <div aria-busy={busy}>
      <div ref={alert} role="alert" tabIndex={-1} hidden={!error}>
        <p>{error}</p>
        {Object.keys(issues).length > 0 && <ul>{Object.keys(issues).map(field => <li key={field}>
          <a href={`#${id}-${field}`} onClick={event => {
            event.preventDefault();
            const input = document.getElementById(`${id}-${field}`);
            const owner = input?.closest("details");
            if (owner) owner.open = true;
            requestAnimationFrame(() => input?.focus());
          }}>{fieldNames[field] ?? field}</a>
        </li>)}</ul>}
      </div>
      <div role="status">{busy ? "处理中…" : message}</div>
      {!catalog && !busy && <button type="button" onClick={() => void perform(() => fetchPage())}>读取材料库</button>}
      {catalog && <>
        <label>库中材料
          <select aria-label="库中材料" value={head?.key ?? ""} disabled={busy} onChange={event => {
            const entryKey = event.target.value;
            void perform(() => loadEntry(entryKey));
          }}>
            <option value="">{readOnly ? "选择材料" : "新建材料"}</option>
            {catalog.items.map(item => <option key={item.key} value={item.key}>
              {item.key} · 版本 {item.revision}{item.archived ? " · 已归档" : ""}
            </option>)}
          </select>
        </label>
        <div className="case-meta-actions">
          {catalog.next_after && <button type="button" disabled={busy} onClick={() => void perform(() => fetchPage(catalog.next_after ?? ""))}>更多材料</button>}
          <button type="button" disabled={busy} onClick={() => void perform(() => fetchPage())}>刷新列表</button>
          {head && <button type="button" disabled={busy} onClick={() => void perform(() => loadEntry(head.key))}>
            {dirty ? "放弃草稿并读取当前材料" : "读取当前材料"}
          </button>}
        </div>
        {head && <div className="case-material-inline">
          <label>材料版本 <input type="number" min={1} max={head.revision} step={1} value={version}
            disabled={busy} onChange={event => setVersion(event.target.value)} /></label>
          <button type="button" disabled={busy || !/^\d+$/.test(version) || Number(version) < 1 || Number(version) > head.revision}
            onClick={() => void perform(async () => {
              const row = await api.get<Entry>(`${path}/${head.key}?revision=${version}`);
              choose(head,row);
            })}>读取版本</button>
        </div>}

        {(!readOnly || selected?.material) && <form aria-label="材料属性" onSubmit={event => {
          event.preventDefault();
          if (!busy && !readonlyFields && slug.test(key)) void perform(save);
        }}>
          {!head && <label>材料标识 <input required pattern="[A-Za-z][A-Za-z0-9_\-]{0,63}" maxLength={64}
            value={key} disabled={busy || readOnly} onChange={event => setKey(event.target.value)} /></label>}
          <fieldset disabled={readonlyFields || busy}>
            <legend>材料属性</legend>
            <div className="case-meta-fields">
              {Object.entries(catalog.schema.enums).map(([field, options]) => <label key={field} htmlFor={`${id}-${field}`}>
                {fieldNames[field] ?? field}
                <select id={`${id}-${field}`} aria-label={fieldNames[field] ?? field} value={String(material[field] ?? "")} aria-invalid={!!issues[field]}
                  aria-describedby={issues[field] ? `${id}-${field}-error` : undefined}
                  onChange={event => change(field,event.target.value)}>
                  <option value="">未指定</option>
                  {options.map(option => <option key={option} value={option}>{optionNames[option] ?? option}</option>)}
                </select>
                {issues[field] && <small id={`${id}-${field}-error`}>{issues[field]}</small>}
              </label>)}
              <label htmlFor={`${id}-confirmed`}>确认状态 <select id={`${id}-confirmed`} aria-label="确认状态" value={String(material.confirmed ?? "")}
                aria-invalid={!!issues.confirmed} aria-describedby={issues.confirmed ? `${id}-confirmed-error` : undefined}
                onChange={event => change("confirmed",event.target.value ? event.target.value === "true" : undefined)}>
                <option value="">未指定</option><option value="true">已确认</option><option value="false">未确认</option>
              </select>{issues.confirmed && <small id={`${id}-confirmed-error`}>{issues.confirmed}</small>}</label>
            </div>
            <details>
              <summary>物性与单位</summary>
              <div className="case-material-properties">
                {Object.entries(catalog.schema.units).map(([field, units]) => {
                  const current = quantity(material,field,units);
                  const label = fieldNames[field] ?? field;
                  return <div key={field} className="case-material-quantity">
                    <label htmlFor={`${id}-${field}`}>{label}<input id={`${id}-${field}`} aria-label={label} type="number" step="any"
                      value={current.value} aria-invalid={!!issues[field]}
                      aria-describedby={issues[field] ? `${id}-${field}-error` : undefined} onChange={event => change(field,event.target.value ?
                        {value:event.target.value,unit:current.unit} : undefined)} />
                      {issues[field] && <small id={`${id}-${field}-error`}>{issues[field]}</small>}
                    </label>
                    <label>{label}单位<select aria-label={`${label}单位`} value={current.unit} disabled={!current.value}
                      onChange={event => change(field,{...current,unit:event.target.value})}>
                      {units.map(unit => <option key={unit} value={unit}>{unit}</option>)}
                    </select></label>
                  </div>;
                })}
              </div>
            </details>
          </fieldset>
          {!readOnly && <div className="case-meta-actions">
            <button type="submit" disabled={busy || readonlyFields || !slug.test(key) || (!dirty && !head?.archived)}>
              {head?.archived ? "恢复到材料库" : "保存材料"}
            </button>
            {head && !head.archived && <button type="button" disabled={busy || historical || dirty} onClick={() => void perform(archive)}>归档材料</button>}
          </div>}
        </form>}

        {!readOnly && head && !head.archived && selected?.material && <form className="case-material-inline" aria-label="引用材料"
          onSubmit={event => {event.preventDefault(); if (!busy && !dirty && slug.test(caseName)) void perform(applySelected);}}>
          <label>算例内材料名 <input required pattern="[A-Za-z][A-Za-z0-9_\-]{0,63}" maxLength={64} value={caseName}
            disabled={busy} onChange={event => setCaseName(event.target.value)} /></label>
          <button type="submit" disabled={busy || dirty || !slug.test(caseName)}>应用版本 {selected.revision} 到算例</button>
        </form>}
      </>}
    </div>
  </details>;
}
