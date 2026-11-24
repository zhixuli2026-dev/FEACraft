import { useId, useState } from 'react';
import type { ContextUsage } from '../utils/contextUsage';

const rows = [
  ['input_tokens', '总输入'], ['uncached_input_tokens', '未缓存输入'],
  ['cache_read_input_tokens', '缓存命中'], ['cache_write_input_tokens', '缓存写入'],
  ['output_tokens', '总输出'], ['reasoning_output_tokens', '推理输出'],
] as const;

export interface ModelUsage {
  status: 'reported' | 'pending' | 'not_reported';
  model: string | null;
  counts: (Record<typeof rows[number][0], number | null> & {total_tokens: number}) | null;
}

export default function ModelContextUsage({ usage, estimate, streaming }: {
  usage?: ModelUsage; estimate: ContextUsage; streaming: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailId = useId();
  const actual = usage?.status === 'reported' ? usage.counts : null;
  const awaiting = streaming && !actual;
  const estimates = `当前文本估算约 ${estimate.estimatedTokens.toLocaleString()} tokens`;
  const title = actual ? rows.map(([key, label]) => `${label} ${actual[key]?.toLocaleString() ?? '未提供'}`).join(' · ')
    : estimates;
  return <section className="agent-context-usage">
    <button type="button" data-testid="context-usage" title={title} aria-expanded={expanded}
      aria-controls={detailId} onClick={() => setExpanded(value => !value)}>
      {actual ? `上次 API 请求 ${actual.total_tokens.toLocaleString()} tokens`
        : awaiting ? '等待 API 用量' : `文本估算约 ${estimate.estimatedTokens.toLocaleString()} tokens`}
    </button>
    <div id={detailId} hidden={!expanded}>
    {actual ? <>
      <div className="agent-context-usage-model">{usage?.model} · 服务商报告</div>
      <dl className="agent-token-breakdown">
        {rows.map(([key, label]) => <div key={key}><dt>{label}</dt>
          <dd data-testid={`usage-${key}`}>{actual[key]?.toLocaleString() ?? '未提供'}</dd></div>)}
      </dl>
      <p>缓存计入输入，推理计入输出。实际费用以服务商账单为准。</p>
    </> : <p>{awaiting ? '请求结束后显示服务商报告。' : '当前会话没有可显示的 API 用量报告。'}</p>}
    <p title="按当前可见正文、工具结果和草稿的字符数除以 4；未计入推理正文或服务商的提示开销。">
      {estimates}（按字符数估算）
    </p>
    </div>
  </section>;
}
