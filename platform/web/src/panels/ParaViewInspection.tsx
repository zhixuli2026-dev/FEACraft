import { useEffect, useState } from "react";

export type InspectionDraft = {
  enabled: boolean; property: string; values: string; columns: number; rows: number; overlay: boolean;
  view: "front" | "isometric" | "top"; material: boolean; color: string; metallic: number; roughness: number;
  opacity: number; coat_strength: number; coat_roughness: number;
  raytrace: string;
};

export const defaultInspection: InspectionDraft = {
  enabled: false, property: "", values: "", columns: 2, rows: 2, overlay: false, view: "isometric",
  material: false, color: "#d9d9d9", metallic: 0, roughness: .3, opacity: 1, coat_strength: 0, coat_roughness: .2,
  raytrace: "",
};

export type InspectionResult = {
  image: string;
  tiles: { x?: number; y?: number; points: number; cells: number }[];
  settings: { comparison: null | { tracks: { node: string; property: string; values: number[] }[] } };
  memory: { process_kib: number; host_used_kib: number; host_total_kib: number; process_limit_kib: number; rank: number };
  raytrace?: { name: string; backend: string; library: string } | null;
  projection?: { mode: string; angle: number; size: [number,number] };
};

const coefficients = [["metallic", "金属度"], ["roughness", "粗糙度"], ["opacity", "不透明度"],
  ["coat_strength", "清漆强度"], ["coat_roughness", "清漆粗糙度"]] as const;

export function inspectionParameters(draft: InspectionDraft) {
  if (!draft.enabled) return undefined;
  const raytrace = draft.raytrace.trim() ? JSON.parse(draft.raytrace) : undefined;
  if (raytrace && (draft.material || draft.property)) throw new Error("使用 OSPRay 时请选择单视图和材质库。");
  let comparison;
  if (draft.property) {
    const values = draft.values.trim().split(/[\s,，]+/).filter(Boolean).map(Number);
    if (values.length !== draft.columns * draft.rows || values.some(value => !Number.isFinite(value))) {
      throw new Error("为每个比较视图填写一个有限数值。");
    }
    const display = draft.property === "@opacity";
    comparison = { dimensions: [draft.columns, draft.rows], overlay: draft.overlay,
      tracks: [{ node: display ? "@display" : "edited", property: display ? "Opacity" : draft.property, values }] };
  }
  const material = draft.material ? {
    color: draft.color.slice(1).match(/../g)?.map(hex => Number.parseInt(hex, 16) / 255),
    metallic: draft.metallic, roughness: draft.roughness, opacity: draft.opacity,
    coat_strength: draft.coat_strength, coat_roughness: draft.coat_roughness,
  } : undefined;
  return { comparison, material, view: draft.view, raytrace };
}

export function InspectionSettings({ draft, fields, locked, update }: {
  draft: InspectionDraft; fields: { name: string; label: string }[]; locked: boolean;
  update: (value: InspectionDraft) => void;
}) {
  return <details className="vf-pv-extracts vf-pv-inspection">
    <summary>比较视图、内存和材质</summary>
    <label className="vf-pv-check"><input type="checkbox" checked={draft.enabled} disabled={locked}
      onChange={e => update({ ...draft, enabled: e.target.checked })} />生成检查结果</label>
    {draft.enabled && <>
      <div className="vf-pv-fields">
        <label>比较属性<select aria-label="比较属性" value={draft.property} disabled={locked}
          onChange={e => update({ ...draft, property: e.target.value })}>
          <option value="">单视图</option><option value="@opacity">显示不透明度</option>
          {fields.map(field => <option value={field.name} key={field.name}>{field.label}</option>)}
        </select></label>
        <label>共同视角<select aria-label="共同视角" value={draft.view} disabled={locked}
          onChange={e => update({ ...draft, view: e.target.value as InspectionDraft["view"] })}>
          <option value="isometric">等轴测</option><option value="front">前视</option><option value="top">俯视</option>
        </select></label>
        {!!draft.property && <>
          <label>列数<input type="number" min={1} max={4} value={draft.columns} disabled={locked}
            onChange={e => update({ ...draft, columns: Number(e.target.value) })} /></label>
          <label>行数<input type="number" min={1} max={4} value={draft.rows} disabled={locked}
            onChange={e => update({ ...draft, rows: Number(e.target.value) })} /></label>
          <label>各格数值<input type="text" value={draft.values} placeholder="0.5, 0.8, 1.1, 1.4" disabled={locked}
            onChange={e => update({ ...draft, values: e.target.value })} /></label>
          <label className="vf-pv-check"><input type="checkbox" checked={draft.overlay} disabled={locked}
            onChange={e => update({ ...draft, overlay: e.target.checked })} />叠加比较</label>
        </>}
      </div>
      <label className="vf-pv-check"><input type="checkbox" checked={draft.material} disabled={locked}
        onChange={e => update({ ...draft, material: e.target.checked })} />使用 PBR 材质</label>
      {draft.material && <div className="vf-pv-fields">
        <label>基色<input type="color" disabled={locked} value={draft.color}
          onChange={e => update({ ...draft, color: e.target.value })} /></label>
        {coefficients.map(([key, label]) => <label key={key}>{label}
          <input type="number" min={0} max={1} step={.01} value={draft[key]} disabled={locked}
            onChange={e => update({ ...draft, [key]: Number(e.target.value) })} />
        </label>)}
      </div>}
      <details className="vf-pv-animation-edit">
        <summary>OSPRay 材质库</summary>
        <label>材质库配置 JSON<textarea aria-label="材质库配置 JSON" rows={4} disabled={locked} value={draft.raytrace}
          placeholder='{"name":"Metal_Copper_normal"}' onChange={e => update({ ...draft, raytrace: e.target.value })} /></label>
      </details>
    </>}
  </details>;
}

export function NativeResultImage({ caseId, path, label, width, height }: {
  caseId: number; path: string; label: string; width: number; height: number;
}) {
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let url = "";
    void (async () => {
      try {
        const token = localStorage.getItem("vf_token");
        const response = await fetch(`/api/v1/cases/${caseId}/raw?path=${encodeURIComponent(path)}`,
          { signal: controller.signal, headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        if (!controller.signal.aborted) { url = URL.createObjectURL(blob); setImage(url); }
      } catch (cause) {
        if (!controller.signal.aborted) setError(`图像读取失败。${cause instanceof Error ? cause.message : cause}`);
      }
    })();
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [caseId, path]);
  if (image) return <img src={image} width={width} height={height} alt={label} />;
  if (error) return <p role="alert">{error}</p>;
  return <p role="status">正在读取{label}…</p>;
}

export function InspectionPreview({ caseId, value, downloadMaterial, downloading }: {
  caseId: number; value: InspectionResult; downloadMaterial: (path: string) => void; downloading: boolean;
}) {
  const [width, height] = value.projection?.size ?? [800,600];
  return <section className="vf-pv-inspection-result" aria-label="ParaView 检查结果">
    <h4>检查结果</h4>
    <NativeResultImage caseId={caseId} path={value.image} label="ParaView 检查视图" width={width} height={height} />
    {value.projection && <button type="button" disabled={downloading} onClick={() => downloadMaterial(value.image)}>下载全景图</button>}
    {value.raytrace && <p>{value.raytrace.name} · {value.raytrace.backend} <button type="button" disabled={downloading}
      onClick={() => value.raytrace && downloadMaterial(value.raytrace.library)}>下载材质库</button></p>}
    {value.settings.comparison && <ul aria-label="各视图参数">{value.tiles.map((tile, index) => <li key={index}>
      <strong>{value.settings.comparison?.tracks.map(track => `${track.property} ${track.values[index]}`).join(" · ")}</strong>
      <span>{tile.points} 点 · {tile.cells} 单元</span>
    </li>)}</ul>}
    <dl>
      {([["渲染进程", value.memory.process_kib], ["进程上限", value.memory.process_limit_kib],
        ["宿主已用", value.memory.host_used_kib], ["宿主总计", value.memory.host_total_kib]] as const).map(([label, size]) =>
        <div key={label}><dt>{label}</dt><dd>{(size / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} MiB</dd></div>)}
    </dl>
  </section>;
}
