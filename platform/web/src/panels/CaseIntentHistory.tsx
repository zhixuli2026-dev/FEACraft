import { useEffect, useState } from "react";
import { api } from "../api";
import "./CaseIntentHistory.css";

const pageSize = 12;
const kinds = {
  openQuestions: "问题",
  assumptions: "假设",
  decisionLog: "决策",
};
const statusNames: Record<string, string> = {
  open: "待解决", resolved: "已解决", proposed: "待确认", confirmed: "已确认",
  accepted: "已采纳", superseded: "已替代", rejected: "已拒绝", withdrawn: "已撤回",
};
type Change = {
  kind: keyof typeof kinds;
  id: string;
  action: "created" | "updated" | "removed";
  from_status: string | null;
  to_status: string | null;
};

type HistoryPage = {
  chain_valid: boolean;
  has_more: boolean;
  intent_lifecycle?: {
    current: Partial<Record<keyof typeof kinds, Record<string, string | null>>>;
    revisions: { event_id: string; revision: number; changes: Change[] }[];
  };
};

function formatStatus(value: string | null) {
  if (!value) return "未填写";
  return Object.prototype.hasOwnProperty.call(statusNames, value) ? statusNames[value] : value;
}

function describeChange(change: Change) {
  if (change.action === "removed") return `移除，原状态 ${formatStatus(change.from_status)}`;
  if (change.action === "created") return `新增，${formatStatus(change.to_status)}`;
  if (change.from_status === change.to_status) return "内容已更新";
  return `${formatStatus(change.from_status)} → ${formatStatus(change.to_status)}`;
}

export default function CaseIntentHistory({ caseId, revision }: { caseId: number; revision: number }) {
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState({ revision, offset: 0 });
  const [retry, setRetry] = useState(0);
  const [page, setPage] = useState<{ key: string; value?: HistoryPage; error?: string }>();
  const offset = selection.revision === revision ? selection.offset : 0;
  const key = `${caseId}:${revision}:${offset}:${retry}`;
  const current = page?.key === key ? page : undefined;
  const lifecycle = current?.value?.intent_lifecycle;

  useEffect(() => {
    if (!open) return;
    let active = true;
    api.get<HistoryPage>(`/cases/${caseId}/provenance?offset=${offset}&limit=${pageSize}`)
      .then(value => { if (active) setPage({ key, value }); })
      .catch(() => { if (active) setPage({ key, error: "无法读取工程意图修订。" }); });
    return () => { active = false; };
  }, [caseId, key, offset, open]);

  function navigate(nextOffset: number) {
    setSelection({ revision, offset: nextOffset });
  }

  return (
    <details className="case-intent-history" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>工程意图修订</summary>
      {open && <div className="case-intent-history-body">
        {!current && <div role="status">读取修订中…</div>}
        {current?.error && <div className="case-intent-history-retry">
          <span role="alert">{current.error}</span>
          <button type="button" onClick={() => setRetry(value => value + 1)}>重试</button>
        </div>}
        {current?.value?.chain_valid === false && <div role="alert">修订链校验失败，无法显示状态变化。</div>}
        {current?.value?.chain_valid && <>
          {lifecycle?.current && Object.keys(lifecycle.current).length > 0 && <dl aria-label="当前状态">
            {Object.entries(lifecycle.current).flatMap(([kind, entries]) =>
              Object.entries(entries).map(([name, status]) => <div key={`${kind}:${name}`}>
                <dt>{kinds[kind as keyof typeof kinds]} · {name}</dt>
                <dd>{formatStatus(status)}</dd>
              </div>)
            )}
          </dl>}
          {lifecycle?.revisions.length ? <ol aria-label="状态变化" className="case-intent-revisions">
            {lifecycle.revisions.map(item => <li key={item.event_id}>
              <strong>修订 {item.revision}</strong>
              <ul>
                {item.changes.map(change => <li key={`${change.kind}:${change.id}`}>
                  <span>{kinds[change.kind]} · {change.id}</span>
                  <span>{describeChange(change)}</span>
                </li>)}
              </ul>
            </li>)}
          </ol> : <div>{offset > 0 ? "本页没有具名条目变更。" : "暂无近期具名条目变更。"}</div>}
          {(offset > 0 || current.value.has_more) && <nav aria-label="修订分页">
            <button type="button" disabled={offset === 0} onClick={() => navigate(Math.max(0, offset - pageSize))}>较新修订</button>
            <button type="button" disabled={!current.value.has_more} onClick={() => navigate(offset + pageSize)}>较早修订</button>
          </nav>}
        </>}
      </div>}
    </details>
  );
}
