import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import "./ReportHistory.css";

interface Version { id: string; generation: number; status: string; current: boolean }

/** Mount per case/path: a late history response cannot enter another document. */
export default function ReportHistory({ caseId, selectedId, onSelect, main }: {
  caseId: number; selectedId?: string; onSelect: (id: string) => void; main?: string;
}) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Version[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [more, setMore] = useState(true);
  const alive = useRef(true);
  const inFlight = useRef(false);
  const retryReset = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  async function load(reset = false) {
    if (inFlight.current) return;
    inFlight.current = true;
    retryReset.current = reset;
    setLoading(true); setError("");
    try {
      const last = !reset && rows.length ? rows[rows.length - 1] : null;
      const query = main ? `latex/publications?limit=20&main=${encodeURIComponent(main)}`
        + (last ? `&before_id=${encodeURIComponent(last.id)}` : "")
        : `report/publications?limit=20` + (last ? `&before_generation=${last.generation}` : "");
      const values = await api.get<Version[]>(`/cases/${caseId}/${query}`);
      if (!alive.current) return;
      if (!Array.isArray(values) || values.some(row => typeof row.id !== "string"
          || !Number.isInteger(row.generation) || row.generation < 1
          || typeof row.current !== "boolean" || typeof row.status !== "string")) {
        throw new Error("版本列表格式无效");
      }
      setRows(previous => reset ? values : [...previous, ...values.filter(row => !previous.some(old => old.id === row.id))]);
      setMore(values.length === 20);
    } catch (cause) {
      if (alive.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      inFlight.current = false;
      if (alive.current) setLoading(false);
    }
  }

  return <div className="report-history">
    <button type="button" aria-expanded={open} data-testid="report-history-toggle"
      onClick={() => { setOpen(value => !value); if (!open && !rows.length) void load(true); }}>历史版本</button>
    {open && <section aria-label="报告历史版本" className="report-history-list">
      <div className="report-history-heading">
        <span>选择版本后可阅读并下载原件；不会替换工作区文件。</span>
        <button type="button" disabled={loading} onClick={() => void load(true)}>刷新列表</button>
      </div>
      <div className="report-history-rows">
        {rows.map(row => <button type="button" key={row.id} data-publication-id={row.id}
          aria-pressed={row.id === selectedId} disabled={!["committed", "superseded"].includes(row.status)}
          onClick={() => onSelect(row.id)}>
          <span>版本 {row.generation}</span>
          <span>{row.status === "committed" ? (row.current ? "最新发布 · 打开时核验" : "历史发布") : row.status === 'superseded' ? '保留产物 · 未成为当前有效结果'
            : row.status === "failed" ? "发布失败" : row.status === "prepared" ? "尚未发布" : "未成为有效发布"}</span>
          {row.id === selectedId && <span>正在阅读</span>}
        </button>)}
      </div>
      {loading && <p role="status">正在读取版本…</p>}
      {error && <p role="alert">历史列表读取失败：{error}。<button type="button"
        disabled={loading} onClick={() => void load(retryReset.current)}>重试</button></p>}
      {!loading && !error && !rows.length && <p>暂无已记录的报告版本。</p>}
      {more && rows.length > 0 && <button type="button" disabled={loading}
        onClick={() => void load()}>加载更早版本</button>}
    </section>}
  </div>;
}
