import { ApiError } from "../api";

const action = {padding: "3px 8px", color: "var(--vf-text)", background: "var(--vf-bg)",
  border: "1px solid var(--vf-border)", borderRadius: 5, cursor: "pointer", fontSize: 12};

export function draftError(error: unknown) {
  return error instanceof ApiError && ["revision_conflict", "file_conflict"].includes(error.code ?? "")
    ? "服务器内容已变化，表单草稿已保留。请查看服务器版本后再提交。"
    : error instanceof Error ? error.message : String(error);
}

export function DraftReview({value, onChoose, allowKeep = true}: {
  value: unknown;
  onChoose: (keep: boolean) => void;
  allowKeep?: boolean;
}) {
  return <div data-testid="form-server-version" style={{marginTop: 8, color: "var(--vf-text)"}}>
    <div>服务器当前内容</div>
    <pre style={{maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12}}>
      {value === undefined ? "条目已不存在" : JSON.stringify(value, null, 2)}
    </pre>
    <div style={{display: "flex", flexWrap: "wrap", gap: 6}}>
      <button type="button" style={action} disabled={!allowKeep} onClick={() => onChoose(true)}>保留草稿，基于此版本继续编辑</button>
      <button type="button" style={action} onClick={() => {
        if (window.confirm("使用服务器版本会放弃此表单的本地草稿，是否继续？")) onChoose(false);
      }}>使用服务器版本</button>
    </div>
  </div>;
}
