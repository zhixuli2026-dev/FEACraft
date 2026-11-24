import { useEffect, useState } from "react";
import { api } from "../api";
import { draftError } from "./CaseDraftFeedback";
import "./CaseIntentHistory.css";

type Publication = {id: string; generation: number; status: string; current: boolean};
type Study = {
  metric: string;
  unit: string;
  levels: {cells: number; value: number; postprocess_publication_id: string}[];
  changes: {from_cells: number; to_cells: number; absolute_change: number; tolerance: number}[];
  finest_pair_within_tolerance: boolean;
};

const number = new Intl.NumberFormat("zh-CN", {maximumSignificantDigits: 10});
const format = (value: number) => value !== 0 && Math.abs(value) < .001 ? value.toExponential(5) : number.format(value);

export default function CaseMeshStudy({caseId}: {caseId: number}) {
  const [open, setOpen] = useState(false);
  const [publications, setPublications] = useState<Publication[]>();
  const [loadingError, setLoadingError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [chosen, setChosen] = useState<string[]>([]);
  const [metric, setMetric] = useState("");
  const [absolute, setAbsolute] = useState("0");
  const [relative, setRelative] = useState("0.01");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [study, setStudy] = useState<Study>();

  useEffect(() => {
    if (!open) return;
    let active = true;
    api.get<Publication[]>(`/cases/${caseId}/postprocess/publications`).then(rows => {
      if (!active) return;
      setPublications(rows.filter(row => ["committed", "superseded"].includes(row.status)));
      setLoadingError("");
    }).catch(failure => { if (active) setLoadingError(draftError(failure)); });
    return () => { active = false; };
  }, [caseId, open, refresh]);

  const available = chosen.filter(id => publications?.some(item => item.id === id));
  const limits = [Number(absolute), Number(relative)];
  const valid = available.length >= 3 && !!metric.trim() && !!absolute.trim() && !!relative.trim()
    && limits.every(value => Number.isFinite(value) && value >= 0) && limits[1] <= 1;

  async function compare() {
    if (!valid || busy) return;
    setBusy(true); setError(""); setStudy(undefined);
    try {
      setStudy(await api.post<Study>(`/cases/${caseId}/mesh/independence`, {
        postprocess_publication_ids: available, metric: metric.trim(), absolute_tolerance: limits[0], relative_tolerance: limits[1],
      }));
    } catch (failure) {
      setError(draftError(failure));
    } finally {
      setBusy(false);
    }
  }

  function download() {
    if (!study) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(study, null, 2)], {type: "application/json"}));
    const link = document.createElement("a");
    link.href = url; link.download = `mesh-study-${caseId}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <details className="case-artifact-trace" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>网格无关性比较</summary>
    {open && <div className="case-intent-history-body">
      <button type="button" disabled={busy} onClick={() => {setPublications(undefined); setLoadingError(""); setRefresh(value => value + 1);}}>刷新后处理版本</button>
      {loadingError && <p role="alert">{loadingError}</p>}
      {!publications && !loadingError && <p role="status">读取后处理版本…</p>}
      {publications && publications.length < 3 && <p>请先生成至少三个不同单元数的后处理版本。</p>}
      {!!publications && publications.length >= 3 && <form onSubmit={event => {event.preventDefault(); void compare();}}>
        <fieldset disabled={busy}>
          <legend>选择 3 至 8 个后处理版本</legend>
          <div style={{maxHeight: 170, overflowY: "auto", display: "grid", gap: 5}}>
            {publications.map(row => <label key={row.id}>
              <input type="checkbox" checked={chosen.includes(row.id)} disabled={!chosen.includes(row.id) && available.length >= 8}
                onChange={event => {
                  setChosen(previous => event.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id));
                  setStudy(undefined);
                }} /> 版本 {row.generation}{row.current ? " · 当前" : " · 历史"}
            </label>)}
          </div>
          <div style={{display: "grid", gap: 6, marginBlock: 8}}>
            <label>指标名称 <input required maxLength={160} placeholder="mean.T@1" value={metric} onChange={event => {setMetric(event.target.value); setStudy(undefined);}} /></label>
            <label>绝对容差 <input required type="number" min="0" step="any" value={absolute} onChange={event => {setAbsolute(event.target.value); setStudy(undefined);}} /></label>
            <label>相对容差（0–1） <input required type="number" min="0" max="1" step="any" value={relative} onChange={event => {setRelative(event.target.value); setStudy(undefined);}} /></label>
          </div>
          <button type="submit" disabled={!valid}>{busy ? "正在比较…" : "比较网格"}</button>
        </fieldset>
      </form>}
      {error && <p role="alert">{error}</p>}
      {study && <section aria-label="网格比较结果">
        <p role="status">最细两级{study.finest_pair_within_tolerance ? "达到容差" : "超出容差"}</p>
        <div style={{overflowX: "auto"}}>
          <table>
            <caption>{study.metric}{study.unit ? `（${study.unit}）` : ""}</caption>
            <thead><tr><th scope="col">单元数</th><th scope="col">实测值</th><th scope="col">与上一级差值</th><th scope="col">容差</th></tr></thead>
            <tbody>{study.levels.map((level, index) => {
              const change = study.changes[index - 1];
              return <tr key={level.postprocess_publication_id}>
                <th scope="row">{number.format(level.cells)}</th><td>{format(level.value)}</td>
                <td>{change ? format(change.absolute_change) : "—"}</td><td>{change ? format(change.tolerance) : "—"}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        <button type="button" onClick={download}>下载比较结果</button>
      </section>}
    </div>}
  </details>;
}
