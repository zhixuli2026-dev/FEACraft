export interface TokenCounts {
  inputTokens: number; cachedInputTokens: number; cacheWriteInputTokens: number;
  outputTokens: number; reasoningOutputTokens: number; totalTokens: number;
}
export interface NativeUsageValue {
  status: 'reported' | 'not_reported' | 'invalid_report' | 'invalid_record';
  seq: number; attempt?: number; last?: TokenCounts; total?: TokenCounts;
  modelContextWindow?: number | null;
}
const rows: [keyof TokenCounts, string][] = [
  ['inputTokens', '输入'], ['cachedInputTokens', '缓存命中'], ['cacheWriteInputTokens', '缓存写入'],
  ['outputTokens', '输出'], ['reasoningOutputTokens', '推理输出'], ['totalTokens', '合计'],
];

export default function NativeUsage({ value, finished }: { value?: NativeUsageValue; finished: boolean }) {
  const reported = value?.status === 'reported' && value.last && value.total;
  const unavailable = value?.status === 'invalid_report' || value?.status === 'invalid_record';
  const detail = value?.last ? rows.map(([key, label]) => `${label} ${value.last![key].toLocaleString()}`).join(' · ') : undefined;
  return <details className="native-usage" data-testid="native-usage">
    <summary title={reported ? `最近模型调用 · ${detail}` : undefined}>Token 用量{reported ? ` · 线程累计 ${value.total!.totalTokens.toLocaleString()}` : unavailable ? ' · 暂不可用' : finished ? ' · 未提供' : ' · 等待报告'}</summary>
    {reported ? <>
      <table><caption>当前回合最近收到的 CLI 报告</caption>
        <thead><tr><th scope="col">Token</th><th scope="col">最近模型调用</th><th scope="col">线程累计</th></tr></thead>
        <tbody>{rows.map(([key, label]) => <tr key={key}><th scope="row">{label}</th><td>{value.last![key].toLocaleString()}</td><td>{value.total![key].toLocaleString()}</td></tr>)}</tbody>
      </table>
      <p className="native-meta">模型上下文窗口：{value.modelContextWindow == null ? '未提供' : `${value.modelContextWindow.toLocaleString()} tokens`}</p>
      <p className="native-meta">线程累计包含继承的历史。缓存计入输入，推理计入输出。实际费用以服务商账单为准。</p>
    </> : <p className="native-meta">{unavailable ? '用量报告无法读取；回复结果仍可查看。' : finished ? 'CLI 没有提供本次尝试的用量，无法确认消耗。' : 'CLI 尚未提供本次尝试的用量。'}</p>}
  </details>;
}
