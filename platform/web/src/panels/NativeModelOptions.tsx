import { useId } from 'react';

export interface NativeModelChoices { effort: string; summary: string; service_tier: string }
export interface NativeModelInfo {
  id: string; model?: string; displayName?: string;
  supportedReasoningEfforts?: { reasoningEffort: string; description: string }[];
  serviceTiers?: { id: string; name: string; description: string }[];
}
export const summaryNames: Record<string, string> = { auto: '自动', concise: '简洁', detailed: '详细', none: '关闭' };

export default function NativeModelOptions({ value, change, model, disabled }: {
  value: NativeModelChoices; change: (value: NativeModelChoices) => void; model?: NativeModelInfo; disabled: boolean;
}) {
  const id = useId();
  const count = Object.values(value).filter(Boolean).length;
  return <details className="native-model-settings" data-testid="native-model-settings">
    <summary>原生模型参数 · 下一条消息{count ? ` · ${count} 项` : ''}</summary>
    <div className="native-model-fields">
      <label className="native-question">推理强度<input aria-label="原生推理强度" list={id + '-efforts'} maxLength={64}
        placeholder="沿用原生设置" disabled={disabled} value={value.effort} onChange={e => change({ ...value, effort: e.target.value })} /></label>
      <datalist id={id + '-efforts'}>{model?.supportedReasoningEfforts?.map(item => <option key={item.reasoningEffort} value={item.reasoningEffort}>{item.description}</option>)}</datalist>
      <label className="native-question">推理摘要<select aria-label="原生推理摘要" disabled={disabled} value={value.summary} onChange={e => change({ ...value, summary: e.target.value })}>
        <option value="">沿用原生设置</option>{Object.entries(summaryNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label className="native-question">服务档位（仅本次）<input aria-label="原生服务档位" list={id + '-tiers'} maxLength={64}
        placeholder="沿用原生设置" disabled={disabled} value={value.service_tier} onChange={e => change({ ...value, service_tier: e.target.value })} /></label>
      <datalist id={id + '-tiers'}>{model?.serviceTiers?.map(item => <option key={item.id} value={item.id}>{item.name} · {item.description}</option>)}</datalist>
      <p className="native-meta">检查运行环境可读取 CLI 模型支持的选项。自定义值需由所选模型支持；留空沿用原生设置。服务档位可能影响费用。</p>
    </div>
  </details>;
}
