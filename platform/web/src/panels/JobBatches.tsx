import { workflowStatusLabel } from '../utils/workflowRecovery';
import type { PreparedBatchOperation } from '../utils/executionLaunch';
import './JobBatches.css';

export interface BatchRow {
  id: string; status: string; total: number; active: number; max_concurrent: number;
  counts: Record<'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted', number>;
  jobs: { id: number; command: string; status: string; phase?: string | null }[];
  operations?: (PreparedBatchOperation & { job_id: number })[];
}

const batchStatus = (row: BatchRow) => workflowStatusLabel(
  row.status === 'cancelled' && row.jobs.some(job => job.phase === 'cancelling') ? 'cancelling' : row.status,
);

export default function JobBatches({ rows, selected, onSelect, onOpenJob, onCancel, cancelling }: {
  rows: BatchRow[]; selected: string | null; onSelect: (id: string) => void; onOpenJob: (id: number) => void;
  onCancel: (id: string) => void; cancelling: ReadonlySet<string>;
}) {
  const current = rows.find(row => row.id === selected);
  const runByJob = new Map(current?.operations?.map(operation => [operation.job_id, operation.run_dir]));
  return <div className="job-batches" data-testid="job-batches">
    <aside className="job-batch-list" aria-label="批次列表">
      {!rows.length && <p>还没有批次。可在“新建任务”中提交多工况求解或批量命令。</p>}
      {rows.map(row => <button key={row.id} type="button" data-batch-id={row.id} aria-pressed={selected === row.id} onClick={() => onSelect(row.id)}>
        <strong>批次 {row.id.slice(0, 8)}</strong><span>{batchStatus(row)} · {row.counts.succeeded}/{row.total}成功</span>
      </button>)}
    </aside>
    <section className="job-batch-detail" aria-label="批次详情">
      {!current ? <p>{selected ? '正在读取所选批次，请核对列表刷新提示。' : '选择批次查看成员状态、日志和恢复入口。'}</p> : <>
        <header><h3>批次 {current.id.slice(0, 8)} · {batchStatus(current)}</h3>
          {current.active > 0 && <button type="button" disabled={cancelling.has('batch:' + current.id)} onClick={() => onCancel(current.id)}>
            {cancelling.has('batch:' + current.id) ? '提交取消中…' : '取消此批次'}</button>}
        </header>
        <p>{current.total}项 · 并发上限{current.max_concurrent} · {current.counts.queued}项排队 · {current.counts.running}项运行</p>
        <p>各成员保留独立作业和尝试；取消受理后仍需等待各成员停止及收尾核验。</p>
        <ol>{current.jobs.map(job => <li key={job.id} data-job-id={job.id}>
          <p><strong>作业 #{job.id}</strong> · {workflowStatusLabel(job.phase ?? job.status)}</p>
          {runByJob.has(job.id) && <p className="job-batch-command">{runByJob.get(job.id)}</p>}
          <p className="job-batch-command">{job.command}</p>
          <button type="button" onClick={() => onOpenJob(job.id)}>查看作业 #{job.id} 日志与恢复</button>
        </li>)}</ol>
      </>}
    </section>
  </div>;
}
