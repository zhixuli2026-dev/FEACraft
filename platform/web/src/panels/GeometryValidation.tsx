import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { checkedPublication, publicationLabel } from "../utils/publicationStatus";
import type { ReportPublication } from "../utils/documentSnapshot";

type Declaration = string | number | boolean;
type ShapeEvidence = {
  name: string;
  topology?: {boundary_edges: number; nonmanifold_edges: number};
};
type GeometryEvidence = {
  schema: string;
  source_sha256?: string;
  declared_geometry?: Record<string, Declaration>;
  summary: {valid: boolean; watertight: boolean; manifold: boolean};
  objects: ShapeEvidence[];
  publication?: ReportPublication;
};

const labels: Record<string, string> = {
  sourceType: "来源类型", topology: "拓扑声明", scaleToMeters: "到米比例",
  symmetryUsed: "采用对称", periodicUsed: "采用周期",
  watertightRequired: "要求水密", manifoldRequired: "要求流形",
};

export default function GeometryValidation({userId, caseId}: {userId: number; caseId: number}) {
  const [report, setReport] = useState<GeometryEvidence>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const current = () => useStore.getState().user?.id === userId && useStore.getState().currentCase?.id === caseId;

  const load = async (latest: boolean) => {
    if (active.current || !current()) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const expectedId = latest ? undefined : report?.publication?.id;
      const query = expectedId ? `?publication_id=${encodeURIComponent(expectedId)}` : "";
      const token = localStorage.getItem("vf_token");
      const response = await fetch(`/api/v1/cases/${caseId}/geometry/validation${query}`, {
        signal: controller.signal, headers: token ? {Authorization: `Bearer ${token}`} : {},
      });
      if (!response.ok) {
        const {detail} = await response.json();
        throw new Error(typeof detail === "string" ? detail : "几何验证暂不可读取。");
      }
      const value = await response.json() as GeometryEvidence;
      if (value.schema !== "vibeflow.geometry.validation.v1" || !value.summary || !Array.isArray(value.objects)) {
        throw new Error("几何验证记录格式不完整，请重新预览。");
      }
      const publication = value.publication?.id || expectedId ? checkedPublication(value.publication, expectedId) : undefined;
      if (!controller.signal.aborted && current()) setReport({...value, publication});
    } catch (cause) {
      if (!controller.signal.aborted && current()) setError(cause instanceof Error ? cause.message : "几何验证暂不可读取。");
    } finally {
      if (active.current === controller) active.current = null;
      if (!controller.signal.aborted && current()) setBusy(false);
    }
  };
  const declarations = Object.entries(report?.declared_geometry ?? {});
  return <details className="geo-downloads" onToggle={event => {
    if (event.currentTarget.open && !report && !active.current) void load(false);
  }}>
    <summary>几何验证</summary>
    <section aria-label="几何验证记录" aria-busy={busy}>
      {report && <>
        <p>{report.publication ? publicationLabel(report.publication) : "旧验证记录"}</p>
        <p>实测水密 {report.summary.watertight ? "通过" : "未通过"} · 实测流形 {report.summary.manifold ? "通过" : "未通过"}</p>
        {!!declarations.length && <dl>
          {declarations.map(([field, value]) => <div key={field}>
            <dt>{labels[field] ?? field}</dt><dd>{String(value)}</dd>
          </div>)}
        </dl>}
        {report.source_sha256 && <p>几何脚本 SHA-256 <code>{report.source_sha256}</code></p>}
        <ul>{report.objects.map((object, index) => <li key={`${object.name}-${index}`}>
          <span>{object.name}</span>
          {object.topology && <span>边界边 {object.topology.boundary_edges} · 非流形边 {object.topology.nonmanifold_edges}</span>}
        </li>)}</ul>
      </>}
      <div className="geo-download-actions">
        <button type="button" disabled={busy} onClick={() => void load(false)}>
          {report?.publication ? "重新核对原验证" : "读取验证"}
        </button>
        {report?.publication && <button type="button" disabled={busy} onClick={() => void load(true)}>读取最新验证</button>}
      </div>
      {busy && <p role="status">正在读取验证记录…</p>}
      {error && <p role="alert">{error}</p>}
    </section>
  </details>;
}
