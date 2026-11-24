import { CAD_STUDIO_BACKGROUNDS, CAD_STUDIO_ENVIRONMENTS, CAD_STUDIO_TONE } from "../utils/cadStudioLighting";
import type { CadStudioLighting, CadStudioPreset } from "../utils/cadStudioLighting";

const lightingSliders = [
  {key: "intensity", label: "环境强度", max: 4, step: 0.1},
  {key: "rotation", label: "环境旋转", max: 360, step: 1},
  {key: "exposure", label: "曝光", max: 3, step: 0.05},
  {key: "shadowIntensity", label: "阴影强度", max: 1, step: 0.05},
  {key: "shadowSoftness", label: "阴影柔度", max: 1, step: 0.05},
] as const;

interface Props {
  settings: CadStudioLighting;
  size: readonly [number, number] | null;
  status: string;
  shadows: boolean;
  onChange: (patch: Partial<CadStudioLighting>) => void;
}

export default function CadStudioLightingControls({settings, size, status, shadows, onChange}: Props) {
  return <div className="geo-render-settings">
    <label>环境图
      <select data-testid="cad-studio-environment" value={settings.environment}
              onChange={event => onChange({environment: event.target.value as CadStudioPreset})}>
        {CAD_STUDIO_ENVIRONMENTS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select>
    </label>
    <label className="geo-cad-toggle">
      <input type="checkbox" data-testid="cad-studio-4k" checked={settings.highResolution}
             disabled={settings.environment === "studio"}
             onChange={event => onChange({highResolution: event.target.checked})} />
      4K 环境图
    </label>
    {size && <output className="geo-render-feedback" data-testid="cad-studio-resolution">{size[0]} × {size[1]}</output>}
    {status && <p className="geo-render-feedback" role="status">{status}</p>}
    <label>背景
      <select data-testid="cad-studio-background" value={settings.background}
              onChange={event => onChange({background: event.target.value as CadStudioLighting["background"]})}>
        {Object.entries(CAD_STUDIO_BACKGROUNDS).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select>
    </label>
    <label>色调映射
      <select data-testid="cad-studio-tone" value={settings.tone}
              onChange={event => onChange({tone: event.target.value as CadStudioLighting["tone"]})}>
        {Object.entries(CAD_STUDIO_TONE).map(([id, [name]]) => <option key={id} value={id}>{name}</option>)}
      </select>
    </label>
    {lightingSliders.map(({key, label, max, step}) => <label className="geo-cad-slider" key={key}>
      <span>{label}</span><output>{settings[key]}{key === "rotation" ? "°" : ""}</output>
      <input type="range" aria-label={label} min={0} max={max} step={step} value={settings[key]}
             data-testid={`cad-studio-${key}`} disabled={key.startsWith("shadow") && !shadows}
             onChange={event => onChange({[key]: event.target.valueAsNumber})} />
    </label>)}
  </div>;
}
