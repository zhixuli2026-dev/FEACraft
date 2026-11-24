import { useState } from "react";
import "./CaseMetaEditor.css";

type Rows = [string, string][];
type Fields = Record<string, unknown>;

const frameKeys = {rotating: "rotatingFrame", moving: "movingFrame", deforming: "deformingFrame"} as const;
const coordinateLabels = {inertial: "惯性坐标", rotating: "旋转坐标", moving: "平移坐标", deforming: "变形坐标"};
const references = {
  referenceLength: "参考长度 (m)", referenceArea: "参考面积 (m²)", referenceDensity: "参考密度 (kg/m³)",
  referenceVelocity: "参考速度 (m/s)", referencePressure: "参考压力 (Pa)", referenceTemperature: "参考温度",
};

const structuredKeys = new Set<string>([
  "coordinateFrame", "gravityEnabled", "gravityVector", "gravityCoordinates", ...Object.values(frameKeys),
  ...Object.keys(references), "referencePressureRole", "referencePressureCell", "referenceTemperatureRole",
]);

export function isStructuredPhysicalField(key: string) {
  return structuredKeys.has(key.trim());
}

function fieldText(value: unknown) {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.join(" ");
  return "";
}

function dictionary(value: unknown): Fields {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Fields : {};
}

function parseField(value?: string): Fields {
  try { return dictionary(JSON.parse(value || "{}")); }
  catch { return {}; }
}

function patchRows(rows: Rows, patch: Record<string, string | undefined>): Rows {
  const next = rows.filter(([name]) => !Object.prototype.hasOwnProperty.call(patch, name.trim()));
  for (const [name, value] of Object.entries(patch)) if (value !== undefined) next.push([name, value]);
  return next;
}

function TextField({label, value, onChange, numeric = false}: {
  label: string; value: unknown; onChange: (value: string) => void; numeric?: boolean;
}) {
  return <label>{label}
    <input aria-label={label} value={fieldText(value)} inputMode={numeric ? "decimal" : "text"}
      onChange={event => onChange(event.target.value)} />
  </label>;
}

function ChoiceField({label, value, options, onChange, empty = "未设置"}: {
  label: string; value: string; options: Record<string, string>; onChange: (value: string) => void; empty?: string;
}) {
  return <label>{label}
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value)}>
      {empty && <option value="">{empty}</option>}
      {value && !(value in options) && <option value={value}>{value}</option>}
      {Object.entries(options).map(([key, title]) => <option value={key} key={key}>{title}</option>)}
    </select>
  </label>;
}

const disciplines = {fluid:"流体", solidMechanics:"固体力学", thermal:"传热", electromagnetic:"电磁",
  acoustic:"声学", continuum:"连续介质"};
type Discipline = keyof typeof disciplines;
type Profile = {title:string; controls:Fields; fields?:Record<string,string>};
const profiles: Record<Discipline,Record<string,Profile>> = {
  fluid: {
    laminar: {title:"不可压层流", controls:{turbulenceModel:"laminar",compressibility:"incompressible",
      flowRegime:"laminar",turbulence:"none",multiphase:"none"}},
    kEpsilon: {title:"不可压标准 k–ε", controls:{turbulenceModel:"kEpsilon",compressibility:"incompressible",
      flowRegime:"turbulent",turbulence:"RANS",multiphase:"none"}},
    perfectGas: {title:"可压缩理想气体", controls:{thermophysicalModel:"perfectGas",compressibility:"compressible",
      flowRegime:"laminar",turbulence:"none",multiphase:"none"}, fields:{material:"气体材料"}},
    VOF: {title:"不可压双相 VOF", controls:{compressibility:"incompressible", flowRegime:"laminar",
      turbulence:"none",multiphase:"VOF",phaseMaterials:{}}},
  },
  solidMechanics: {linearElastic: {title:"小应变静态线弹性", controls:{materialLaw:"linearElastic",
    kinematics:"smallStrain",dynamics:"static"}, fields:{material:"弹性材料",nativeMaterial:"原生材料名称"}}},
  thermal: {
    conduction: {title:"材料导热", controls:{transport:"conduction"}, fields:{material:"导热材料"}},
    conjugate: {title:"区域材料共轭传热", controls:{transport:"conjugate",materialSource:"regions"}},
  },
  electromagnetic: {
    electrostatic: {title:"静电介质", controls:{formulation:"electrostatic"}, fields:{material:"介质材料"}},
    jouleHeating: {title:"焦耳热耦合", controls:{coupling:"jouleHeating"}},
  },
  acoustic: {pressureWave: {title:"压力波", controls:{formulation:"pressureWave",soundSpeed:{}}}},
  continuum: {continuum:{title:"连续介质",controls:{model:"continuum"},fields:{conservation:"守恒方程"}}},
};

function selectedProfile(group: Discipline, model: Fields) {
  if (group === "fluid") {
    if (model.thermophysicalModel === "perfectGas") return "perfectGas";
    if (model.multiphase === "VOF" && "phaseMaterials" in model) return "VOF";
    return fieldText(model.turbulenceModel);
  }
  if (group === "electromagnetic" && model.coupling === "jouleHeating") return "jouleHeating";
  return fieldText(model[{fluid:"turbulenceModel",solidMechanics:"materialLaw",thermal:"transport",
    electromagnetic:"formulation",acoustic:"formulation",continuum:"model"}[group]]);
}

function GoverningFields({current, disabled, onChange}: {
  current: Record<string,string>; disabled: boolean; onChange: (patch: Record<string,string>) => void;
}) {
  const [group,setGroup] = useState<Discipline>("fluid");
  const all = parseField(current.governingModels);
  const model = dictionary(all[group]);
  const kind = selectedProfile(group,model);
  const available = profiles[group];
  const selected = available[kind];
  const put = (next: Fields, extra: Record<string,string> = {}) =>
    onChange({governingModels:JSON.stringify({...all,[group]:next}),...extra});
  const edit = (changed: Fields) => put({...model,...changed});
  const choose = (key: string) => {
    const profile = available[key];
    if (!profile) return;
    const owned = new Set(Object.values(available).flatMap(option=>[
      ...Object.keys(option.controls),...Object.keys(option.fields || {}),
    ]));
    if (group === "fluid") for (const field of ["turbulenceCoefficients","phaseOrder","surfaceTension"]) owned.add(field);
    const preserve = new Set(Object.keys(profile.fields || {}));
    const next = Object.fromEntries(Object.entries(model).filter(([field]) => preserve.has(field) || !owned.has(field)));
    const coupled: Record<string,string> = key === "jouleHeating" ? {coupledPhysics:JSON.stringify({
      ...parseField(current.coupledPhysics),electromagnetics:true,heatTransfer:true})} : {};
    put({...next,enabled:true,...profile.controls},coupled);
  };
  const scalar = (label:string,key:string) =>
    <TextField label={label} value={model[key]} key={key} onChange={value=>edit({[key]:value || undefined})} />;
  const quantity = (label:string,key:string,units:Record<string,string>,fallback:string) => {
    const amount = dictionary(model[key]);
    return <>
      <TextField label={label} value={amount.value} numeric onChange={value=>edit({[key]:{...amount,value,unit:amount.unit || fallback}})} />
      <ChoiceField label={`${label}单位`} value={fieldText(amount.unit) || fallback} options={units} empty=""
        onChange={unit=>edit({[key]:{...amount,unit}})} />
    </>;
  };
  const phaseMaterials = dictionary(model.phaseMaterials);
  const phaseOrder = fieldText(model.phaseOrder).trim().replace(/^\(|\)$/g, "").split(/\s+/).filter(Boolean);
  return <details>
    <summary>控制模型</summary>
    <fieldset disabled={disabled} className="case-meta-fields">
      <ChoiceField label="物理学科" value={group} options={disciplines} empty="" onChange={value=>setGroup(value as Discipline)} />
      <ChoiceField label="控制模型" value={kind} options={Object.fromEntries(Object.entries(available).map(([key,item])=>[key,item.title]))}
        empty={kind ? "" : "未设置"} onChange={choose} />
      {selected?.fields && Object.entries(selected.fields).map(([key,label])=>scalar(label,key))}
      {kind==="kEpsilon" && <TextField label="Cmu" value={dictionary(model.turbulenceCoefficients).Cmu} numeric
        onChange={value=>edit({turbulenceCoefficients:{...dictionary(model.turbulenceCoefficients),Cmu:value || undefined}})} />}
      {kind==="VOF" && <>
        {scalar("相顺序", "phaseOrder")}
        {phaseOrder.map((phase,index)=><TextField key={`${phase}-${index}`} label={`${phase} 材料`} value={phaseMaterials[phase]}
          onChange={value=>edit({phaseMaterials:{...phaseMaterials,[phase]:value || undefined}})} />)}
        {quantity("表面张力","surfaceTension",{"N/m":"N/m","mN/m":"mN/m"},"N/m")}
      </>}
      {kind==="pressureWave" && quantity("声速","soundSpeed",{"m/s":"m/s","km/s":"km/s"},"m/s")}
    </fieldset>
    {kind==="VOF" && <p>按原生求解器顺序填写两个相名，例如 (water air)，再填写各自的确认材料。</p>}
    {group==="continuum" && <p>方程需与求解器匹配，例如 (mass momentum energy)。</p>}
  </details>;
}

export default function CasePhysicalFields({rows, disabled, onEdit, temperatureUnit}: {
  rows: Rows; disabled: boolean; onEdit: (rows: Rows) => void; temperatureUnit?: string;
}) {
  const current = Object.fromEntries(rows.map(([name, value]) => [name.trim(), value]));
  const mode = current.coordinateFrame || "";
  const gravity = current.gravityEnabled?.toLowerCase() || "";
  const activeKey = frameKeys[mode as keyof typeof frameKeys];
  const coordinates = parseField(current[activeKey]);
  const rotation = dictionary(coordinates.angularVelocity);
  const change = (key: string, value: string) => onEdit(patchRows(rows, {[key]: value || undefined}));
  const changeMode = (next: string) => {
    const patch: Record<string, string | undefined> = {coordinateFrame: next || undefined};
    for (const [frame, key] of Object.entries(frameKeys)) patch[key] = frame === next ? (current[key] || "{}") : undefined;
    onEdit(patchRows(rows, patch));
  };
  const updateFrame = (changed: Fields) => change(activeKey, JSON.stringify({...coordinates, ...changed}));
  const frameField = (label: string, key: string, numeric = false) =>
    <TextField key={key} label={label} value={coordinates[key]} numeric={numeric}
      onChange={value => updateFrame({[key]: value || undefined})} />;
  const plain = (label: string, key: string, numeric = false) =>
    <TextField key={key} label={label} value={current[key]} numeric={numeric} onChange={value => change(key, value)} />;

  return <div className="case-meta-editor" data-testid="physical-problem-fields">
    <fieldset disabled={disabled}>
      <legend>坐标与重力</legend>
      <div className="case-meta-fields">
        <ChoiceField label="坐标系" value={mode} options={coordinateLabels} onChange={changeMode} />
        <ChoiceField label="重力" value={gravity} options={{true: "启用", false: "关闭"}}
          onChange={value => change("gravityEnabled", value)} />
        {gravity === "true" && <>
          <ChoiceField label="重力分量坐标" value={current.gravityCoordinates || ""}
            options={{global: "全局坐标", geometry: "几何坐标"}} empty="默认全局坐标"
            onChange={value => change("gravityCoordinates", value)} />
          {plain("重力矢量 (m/s²)", "gravityVector")}
        </>}
        {mode === "rotating" && <>
          {frameField("旋转单元区", "cellZone")}
          {frameField("旋转原点 (m)", "origin")}
          {frameField("旋转轴", "axis")}
          <TextField label="角速度" value={rotation.value} numeric
            onChange={value => updateFrame({angularVelocity: {...rotation, value, unit: rotation.unit || "rad/s"}})} />
          <ChoiceField label="角速度单位" value={fieldText(rotation.unit) || "rad/s"} options={{"rad/s": "rad/s", rpm: "rpm"}} empty=""
            onChange={unit => updateFrame({angularVelocity: {...rotation, unit}})} />
          {frameField("静止边界", "nonRotatingPatches")}
        </>}
        {mode === "moving" && frameField("运动区域", "region")}
        {mode === "deforming" && <>
          {frameField("移动壁面", "movingPatch")}
          {frameField("固定壁面", "fixedPatch")}
          {frameField("自由边界", "freePatches")}
          {frameField("位移幅值 (m)", "amplitude")}
          {frameField("角频率 (rad/s)", "angularFrequency", true)}
        </>}
      </div>
      {(mode === "rotating" || mode === "deforming" || gravity === "true") &&
        <p>矢量填写三个分量，例如 (0 0 1)。多个边界可填写 (inlet outlet)。</p>}
    </fieldset>
    <details>
      <summary>参考量</summary>
      <fieldset disabled={disabled} className="case-meta-fields">
        {Object.entries(references).map(([key, label]) => plain(key === "referenceTemperature"
          ? `${label} (${temperatureUnit === "degC" ? "°C" : "K"})` : label, key, key !== "referenceVelocity"))}
        <ChoiceField label="参考压力用途" value={current.referencePressureRole || ""} options={{gauge: "表压基准"}}
          onChange={value => change("referencePressureRole", value)} />
        {current.referencePressureRole === "gauge" && plain("压力基准单元", "referencePressureCell", true)}
        <ChoiceField label="参考温度用途" value={current.referenceTemperatureRole || ""} options={{buoyancy: "浮力基准"}}
          onChange={value => change("referenceTemperatureRole", value)} />
      </fieldset>
    </details>
    <GoverningFields current={current} disabled={disabled} onChange={patch=>onEdit(patchRows(rows,patch))} />
  </div>;
}
