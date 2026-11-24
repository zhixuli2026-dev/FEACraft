import { useEffect, useEffectEvent, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, olderConversationPage } from "../api";
import { captureProjectCase, clearSubmittedAgentDraft, currentAgentDraft, useStore } from "../store";
import { RecordedVoice, VoiceController, voiceError } from "../utils/voiceInput";
import type { VoiceSession, VoiceState } from "../utils/voiceInput";
import { diffLines, type LineDiff } from "../utils/diff";
import ChatMarkdown from "./ChatMarkdown";
import NativeChatPanel from "./NativeChatPanel";
import AcpChatPanel, { type AcpHarness } from "./AcpChatPanel";
import type { NativeModelChoices } from "./NativeModelOptions";
import { providerProfilesChanged } from "./ProviderProfilesPanel";
import ModelContextUsage, { type ModelUsage } from "./ModelContextUsage";
import MessageImages, { DraftImages, maxMessageImages, readImageFiles, type ImageAttachment } from './ChatImages';
import { filterDisplayMessages } from "../utils/chatContent";
import { estimateContextUsage } from "../utils/contextUsage";
import { formatAttachment, parsePathReferences } from "../utils/pathRefs";
import { TOOL_KIND_ICONS, toolPresentation, toolStatusLabel } from "../utils/toolPresentation";
import { projectRunTimings, formatRunDuration, runDurationMs, type RunTiming } from "../utils/runTiming";
import { projectToolCards, toolCallKey, upsertToolCard, type ToolCardState } from "../utils/toolCards";
import { playReplySound, prepareReplyAudio, replySoundEnabled, setReplySoundEnabled } from "../utils/replySound";

interface Profile { id: number; provider: string; name: string; base_url: string; default_model: string; has_key: boolean; shared?: boolean }
interface Conv { id: number; case_id: number; title: string; model: string; provider_profile_id: number; permission_mode?: string }
type Backend = "api" | "windows" | "wsl" | "docker";
type ModelPreference = { provider_profile_id: number; model: string; reasoning_effort?: string; native_options?: Partial<NativeModelChoices> };
type EnvironmentPreferences = { preferences: Partial<Record<Backend, ModelPreference>>; stale: Backend[] };
const defaultNativeChoices: NativeModelChoices = { effort: "", summary: "", service_tier: "" };
interface Msg { id?: number; run_id?: string; canFork?: boolean; role: string; content: string; reasoning?: string; cancelled?: boolean; interrupted?: boolean; failure?: "failed" | "timed_out"; tool_calls?: any[]; images?: ImageAttachment[] }
interface PendingTool { id: string; tool: string; args: Record<string, any> }
interface Goal { goal_id: string; objective: string; acceptance_criteria: string[];
  budget: { max_turns: number; max_tool_calls: number }; status: string; stop_reason: string; revision: number }
type ToolCard = ToolCardState;
interface ActiveRun {
  key: string;
  controller: AbortController;
  text: string;
  reasoning: string;
  cancelled: boolean;
  runId: string | null;
  lastSoundAnswerId?: number;
  usage?: ModelUsage;
}
type RunNotice = { kind: "frame"; event: any } | { kind: "finished" }
  | { kind: "accepted"; message: Msg } | { kind: "error"; status: string };
const ACTIVE_RUNS = new Map<string, ActiveRun>();
const RUN_VIEWS = new Map<string, (run: ActiveRun, notice: RunNotice) => void>();
const SELECTED_CONVERSATIONS = new Map<string, number>();
const runKey = (scope: string | null, cid: number) => `${scope}:${cid}`;

function notifyRun(run: ActiveRun, notice: RunNotice) {
  RUN_VIEWS.get(run.key)?.(run, notice);
}

function receiveRunFrame(run: ActiveRun, event: any, userId: number | undefined) {
  if (ACTIVE_RUNS.get(run.key) !== run) return;
  if (event.type === "ready" && event.run_id) run.runId = event.run_id;
  if (event.type === "request.usage") run.usage = event.usage;
  if (event.type === "text.delta") run.text += event.text;
  if (event.type === "reasoning.delta") run.reasoning += event.text;
  if (event.type === "run.cancelled") run.cancelled = true;
  if (event.type === "request.failed" || event.type === "stream.error") {
    run.text += `\n\n[错误] ${event.detail ?? event.message ?? ""}`;
  }
  notifyRun(run, {kind: "frame", event});
  if (event.type === "run.completed") {
    const answerId = event.answer_event_id;
    if (Number.isSafeInteger(answerId) && run.lastSoundAnswerId !== answerId) {
      run.lastSoundAnswerId = answerId;
      if (!run.cancelled) playReplySound(userId);
    }
    run.text = "";
    run.reasoning = "";
    run.cancelled = false;
  }
}

type ApprovalDiff =
  | ({ kind: "text"; creating: boolean } & LineDiff)
  | { kind: "stale" | "unavailable" | "binary" | "limit"; message: string };

interface ProposalFile {
  kind: "text" | "binary";
  content?: string;
  content_sha256?: string;
  size: number;
}

const staleApproval = (): ApprovalDiff => ({
  kind: "stale", message: "文件已偏离提案时的版本。请拒绝此提案，重新读取文件后再生成提案。",
});

async function readApprovalDiff(caseId: number, tc: PendingTool, path: string): Promise<ApprovalDiff> {
  const baseline = tc.args.baseline_sha256;
  if (baseline !== null && (typeof baseline !== "string" || !/^[a-f0-9]{64}$/.test(baseline))) {
    return { kind: "stale", message: "这份提案没有可核验的文件版本。请拒绝后重新生成提案。" };
  }
  const query = `?path=${encodeURIComponent(path)}`;
  const version = () => api.get<{ exists: boolean; content_sha256: string | null }>(`/cases/${caseId}/file:version${query}`);
  let current = "";
  if (baseline === null) {
    if ((await version()).exists) return staleApproval();
  } else {
    let file: ProposalFile;
    try {
      file = await api.get<ProposalFile>(`/cases/${caseId}/file${query}`);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.code === "file_changed_during_read")) return staleApproval();
      throw error;
    }
    if (file.kind === "binary") {
      if ((await version()).content_sha256 !== baseline) return staleApproval();
      return { kind: "binary", message: tc.tool === "propose_file_update"
        ? `当前文件为二进制（${file.size.toLocaleString()} 字节）。批准后会以文本替换原文件。`
        : "当前文件为二进制，无法计算文本替换的逐行差异。" };
    }
    if (file.content_sha256 !== baseline) return staleApproval();
    if (typeof file.content !== "string") throw new Error("文件正文不可用");
    current = file.content;
  }

  return proposalTextDiff(tc, current, baseline === null);
}

function proposalTextDiff(tc: Pick<PendingTool, "tool" | "args">, current: string, creating: boolean): ApprovalDiff {
  let proposal: string;
  if (tc.tool === "propose_file_update") {
    if (typeof tc.args.content !== "string") return { kind: "unavailable", message: "提案缺少文本正文。" };
    proposal = tc.args.content;
  } else {
    const { old_text: oldText, new_text: newText, replace_all: replaceAll } = tc.args;
    if (typeof oldText !== "string" || !oldText || typeof newText !== "string") return { kind: "unavailable", message: "提案缺少可预览的替换文本。" };
    const first = current.indexOf(oldText);
    if (first < 0 || (!replaceAll && current.includes(oldText, first + oldText.length))) {
      return { kind: "unavailable", message: "提案中的原文不符合替换要求。" };
    }
    // Python applies a literal replacement, including strings such as $& and $1.
    proposal = replaceAll ? current.replaceAll(oldText, () => newText) : current.replace(oldText, () => newText);
  }
  const diff = diffLines(current, proposal);
  return diff ? { kind: "text", creating, ...diff }
    : { kind: "limit", message: "改动范围较大，可查看提案参数核对内容。" };
}

function DiffCounts({ diff }: { diff: Extract<ApprovalDiff, { kind: "text" }> }) {
  return <span data-testid="approval-diff-counts" data-added={diff.added} data-removed={diff.removed}
    aria-label={`新增 ${diff.added} 行，删除 ${diff.removed} 行`} style={{ color: "var(--vf-text)", marginInlineStart: 8 }}>
    +{diff.added} −{diff.removed}</span>;
}

function DiffPreview({ diff }: { diff: Extract<ApprovalDiff, { kind: "text" }> }) {
  return <>
    {diff.rows.length ? (
      <pre style={{ ...s.toolPre, maxHeight: 260, background: "#0b0d12", padding: 6, borderRadius: 6 }}>
        {diff.rows.map((row, index) => (
          <span key={index} data-testid="approval-diff-row" data-kind={row.kind} style={{ display: "block", minHeight: "1em",
            color: row.kind === "add" ? "#4ade80" : row.kind === "del" ? "#f87171" : "#abb8cb",
            background: row.kind === "add" ? "#14532d33" : row.kind === "del" ? "#7f1d1d33" : "transparent",
          }}>{row.kind === "add" ? "+ " : row.kind === "del" ? "− " : "  "}{row.text}
            {row.noFinalNewline && row.kind !== "same" && <span title="该行没有结尾换行"> [无结尾换行]</span>}</span>
        ))}
      </pre>
    ) : <div>没有文本改动。</div>}
    {diff.truncated && <div>预览只显示前 400 行差异，增删统计包含全部改动。</div>}
  </>;
}

function ProposalHistory({ card }: { card: ToolCard }) {
  const [expanded, setExpanded] = useState(false);
  const args = card.args;
  const path = args?.path;
  const snapshot = args?.proposal_snapshot;
  const creating = args?.baseline_sha256 === null;
  const diff = useMemo(() => {
    if (!expanded) return undefined;
    const before = typeof snapshot?.before === "string" && snapshot.kind === "text" ? snapshot.before : creating ? "" : undefined;
    return before === undefined ? undefined : proposalTextDiff({ tool: card.tool, args }, before, creating);
  }, [args, card.tool, creating, expanded, snapshot]);
  if (typeof path !== "string" || !path) return null;
  const reason = typeof args.reason === "string" ? args.reason : "";
  return <div data-testid="proposal-history" style={{ color: "var(--vf-text)", fontSize: 12, overflowWrap: "anywhere" }}>
    {!card.result?.slice(0, 800).includes(path) && <code>{path}</code>}
    <details style={{ marginBlockStart: 4 }} onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary style={{ cursor: "pointer", paddingBlock: 3 }}>查看提案{diff?.kind === "text" && <DiffCounts diff={diff} />}</summary>
      {expanded && <>
        {reason && <div data-testid="proposal-history-reason">{reason}</div>}
        {diff?.kind === "text" ? <DiffPreview diff={diff} /> : <>
          <div data-testid="proposal-history-status" style={{ color: "var(--vf-muted)" }}>{diff?.message ?? (snapshot?.kind === "binary"
            ? "提案针对二进制文件，以下为拟写入内容。" : snapshot?.kind === "limit"
              ? "原文件较大，历史保留提案参数。" : "此提案的历史差异不可用。")}</div>
          <pre style={s.toolPre}>{card.tool === "propose_file_update" && typeof args.content === "string" ? args.content :
            JSON.stringify({ 原文: args.old_text, 替换为: args.new_text, 全部替换: args.replace_all }, null, 1)}</pre>
        </>}
      </>}
    </details>
  </div>;
}

/** Proposal previews are tied to the pending request's original file version. */
function ApprovalCard({ tc, busy, onDecide }: {
  tc: PendingTool; busy: boolean; onDecide: (tc: PendingTool, ok: boolean) => void;
}) {
  const caseId = useStore(state => state.currentCase?.id);
  const userId = useStore(state => state.user?.id);
  const path = typeof tc.args.path === "string" ? tc.args.path : "";
  const versionHint = useStore(state => state.fileTree.find(entry => entry.path === path)?.modified);
  const [diff, setDiff] = useState<ApprovalDiff>();
  const [retry, setRetry] = useState(0);
  const isProposal = tc.tool === "propose_file_update" || tc.tool === "propose_text_replacement";
  useEffect(() => {
    if (!isProposal) return;
    if (!caseId || !userId || !path) {
      setDiff({ kind: "unavailable", message: "缺少可核验的提案路径。" });
      return;
    }
    let active = true;
    setDiff(undefined);
    void readApprovalDiff(caseId, tc, path).then(result => {
      if (active) setDiff(result);
    }).catch(() => {
      if (active) setDiff({ kind: "unavailable", message: "暂时无法核验提案文件，请重读差异。" });
    });
    return () => { active = false; };
  }, [caseId, userId, tc.id, path, isProposal, versionHint, retry]);

  const textDiff = diff?.kind === "text" ? diff : undefined;
  const showArguments = diff?.kind === "limit" || diff?.kind === "binary";
  const canApprove = !isProposal || Boolean(diff && diff.kind !== "stale" && diff.kind !== "unavailable");
  const approvalDisabled = busy || !canApprove;

  return (
    <div data-testid="approval-card" data-tool={tc.tool} style={s.approval}>
      <div style={{ color: "#fbbf24", fontSize: 12, marginBottom: 4 }}>
        Agent 请求执行危险操作：<b>{tc.tool}</b>
        {textDiff && <DiffCounts diff={textDiff} />}
      </div>
      {isProposal && <div style={{ overflowWrap: "anywhere" }}>{textDiff?.creating && "新建文件 · "}<code>{path}</code></div>}
      {tc.args.reason && <div style={{ fontSize: 12, color: "#c9a86a", marginBottom: 4 }}>理由：{tc.args.reason}</div>}
      {isProposal && !diff && <div role="status">正在核验提案文件版本…</div>}
      {diff && diff.kind !== "text" && <div data-testid="approval-diff-status" data-state={diff.kind} role="status">
        {diff.message}
        {diff.kind === "unavailable" && path && <button type="button" style={s.ghostBtn} onClick={() => setRetry(value => value + 1)}>重读差异</button>}
      </div>}
      {textDiff && <DiffPreview diff={textDiff} />}
      {showArguments && <details><summary>查看提案参数</summary>
        <pre style={s.toolPre}>{JSON.stringify(Object.fromEntries(Object.entries(tc.args)
          .filter(([key]) => key !== "proposal_snapshot")), null, 1)}</pre></details>}
      {!isProposal && <pre style={s.toolPre}>{JSON.stringify(tc.args, null, 1).slice(0, 600)}</pre>}
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button type="button" style={{ ...s.approveBtn, ...(approvalDisabled ? s.disabledApprovalBtn : {}) }}
          onClick={() => onDecide(tc, true)} disabled={approvalDisabled}>批准执行</button>
        <button type="button" style={s.rejectBtn} onClick={() => onDecide(tc, false)} disabled={busy}>拒绝</button>
      </div>
    </div>
  );
}

/** 仅展示 provider 实际返回的 reasoning；流式展开、完成折叠，不注入最终回答。 */
function ReasoningCard({ text, streaming }: { text: string; streaming: boolean }) {
  const [open, setOpen] = useState(streaming);
  const contentId = useId();
  useEffect(() => { setOpen(streaming); }, [streaming]);
  if (!text) return null;
  return (
    <div data-testid="reasoning-card" data-streaming={streaming} style={s.reasoning}>
      <button type="button" data-testid="reasoning-toggle" aria-expanded={open} aria-controls={contentId}
              style={s.reasoningToggle} onClick={() => setOpen((wasOpen) => !wasOpen)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>{" "}
        推理过程（非最终回答）{streaming ? " · 进行中" : ""}
      </button>
      <div id={contentId} hidden={!open} data-testid="reasoning-content" style={s.reasoningContent}>
        {open ? text : null}
      </div>
    </div>
  );
}

function RunDurationLabel({ timing }: { timing: RunTiming }) {
  const [now, setNow] = useState(() => Date.now());
  const active = timing.status === "started";
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return <span data-testid="run-duration" data-run-id={timing.runId} data-status={timing.status}
               style={{ marginLeft: 6, fontWeight: 400, color: "#8b93a7" }}>
    · {formatRunDuration(runDurationMs(timing, now))}
  </span>;
}

function projectConversationEvents(events: any[], toolCallRecords: any[] = []) {
  const projected: Msg[] = [];
  const localCheckpoints = new Map<string, {content?: string; reasoning?: string}>();
  for (const event of events) {
    const payload = event.payload;
    const run = payload.run_id;
    if (event.type === "message") {
      projected.push({id: event.id, run_id: run, canFork: event.can_fork, role: payload.role,
        content: payload.content ?? "", reasoning: payload.reasoning ?? "", cancelled: payload.cancelled ?? false,
        tool_calls: payload.tool_calls ?? undefined, images: payload.images ?? undefined});
      if (payload.role === "assistant") localCheckpoints.delete(run);
    } else if (event.type === "system" && (payload.kind === "summary" || payload.kind === "handoff")) {
      projected.push({role: "summary", content: payload.text ?? ""});
    } else if (event.type === "run_checkpoint" && run) {
      localCheckpoints.set(run, payload);
    } else if (event.type === "run_state" && run) {
      if (["interrupted", "failed", "timed_out"].includes(payload.status)) {
        const partial = "recovered_checkpoint" in event ? event.recovered_checkpoint : localCheckpoints.get(run);
        if (partial?.content || partial?.reasoning) {
          projected.push({role: "assistant", content: partial.content ?? "", reasoning: partial.reasoning ?? "",
            run_id: run, interrupted: true, canFork: false});
        }
      }
      localCheckpoints.delete(run);
    }
  }
  const states = new Map<string, string>();
  const timingEvents = [...events, ...events.filter(e => e.answer_state).map(e => ({
    type: "run_state", payload: e.answer_state,
  }))];
  for (const event of timingEvents) {
    if (event.type === "run_state" && event.payload.run_id) {
      states.set(event.payload.run_id, event.payload.status);
    }
  }
  for (const message of projected) {
    if (!message.run_id) continue;
    const status = states.get(message.run_id);
    if (status === "cancelled") message.cancelled = true;
    if (status === "interrupted" || status === "failed" || status === "timed_out") {
      message.interrupted = true;
      if (status !== "interrupted") message.failure = status;
    }
  }
  const messages = filterDisplayMessages(projected);
  return { messages, tools: projectToolCards(events, toolCallRecords),
           runTimings: projectRunTimings(timingEvents) };
}

const planStatus = {pending: "待办", in_progress: "进行中", completed: "已完成"} as const;
type PlanStep = {title: string; status: keyof typeof planStatus};

function planSteps(value: unknown): PlanStep[] | null {
  if (!Array.isArray(value) || !value.every((step) => step && typeof step.title === "string" && step.title.trim() &&
      Object.hasOwn(planStatus, step.status))) return null;
  if (value.filter((step) => step.status === "in_progress").length > 1) return null;
  return value;
}

function PlanStrip({ steps }: { steps: PlanStep[] | null }) {
  if (!steps?.length) return null;
  return (
    <ul aria-label="任务计划" data-testid="plan-strip" style={s.planStrip}>
      {steps.map(({title, status}, index) => (
        <li key={index} data-status={status} style={s.planStep}>
          <span style={{ color: status === "in_progress" ? "var(--vf-accent)" : "var(--vf-muted)" }}>{planStatus[status]}</span>
          {title}
        </li>
      ))}
    </ul>
  );
}

function GoalStrip({ goal, onRun, busy }: { goal: Goal | null; onRun: () => void; busy: boolean }) {
  if (!goal) return null;
  const status = ({active: "进行中", completed: "已完成", cancelled: "已取消", blocked: "受阻"} as Record<string, string>)[goal.status] ?? goal.status;
  return (
    <div role="region" aria-label="会话目标" data-testid="goal-strip" data-status={goal.status} data-revision={goal.revision} style={s.goalStrip}>
      <div>{goal.objective} · {status} · 预算 {goal.budget.max_turns} 轮/{goal.budget.max_tool_calls} 次工具调用</div>
      {goal.acceptance_criteria.length > 0 && <div>验收：{goal.acceptance_criteria.join("；")}</div>}
      {goal.stop_reason && <div>停止原因：{goal.stop_reason}</div>}
      {goal.status === "active" && <button data-testid="goal-run" style={s.goalRunBtn} disabled={busy} onClick={onRun}>执行目标</button>}
    </div>
  );
}

/** 解析后端 SSE 流（fetch ReadableStream 手动分包） */
async function consumeSSE(
  res: Response,
  onEvent: (ev: any) => void
) {
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const f of frames) {
      const line = f.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      try { onEvent(JSON.parse(line.slice(5).trim())); } catch {}
    }
  }
}

/** 六阶段快捷提示词（注入到输入框；自有文案，语义对齐桌面版阶段按钮） */
const STAGE_PROMPTS: [string, string][] = [
  ["A几何", "生成或更新 A-geo 几何：读 caseDict 的 geometry 段，编写/修改 A-geo/geometry.py 并预览验证"],
  ["B网格", "推进 B-mesh 网格阶段：确认几何产物可用，写网格配置（blockMeshDict 等）并提交网格作业"],
  ["C求解", "推进 C-run 求解阶段：准备 C-run/case-N 目录与字典，提交求解作业并跟踪残差收敛"],
  ["D后处理", "推进 D-post 后处理：按 postProcessing.objectives 出指标、曲线（postprocess.py）与场景图（scenes.py）"],
  ["E报告", "生成或更新 E-report 报告：汇总证据写 E-report/report.md 并构建产物"],
  ["V开发", "进入 UDF 开发模式：按 D1 需求→D2 设计→D3 实现→D4 wmake 编译验证→D5 集成推进 UDF/"],
];

export default function ChatPanel() {
  const { currentCase, user } = useStore();
  const caseId = currentCase?.id;
  const userId = user?.id;
  const conversationScope = caseId && userId ? `${userId}:${caseId}` : null;
  const [executionBackend, setBackend] = useState<Backend | AcpHarness>("api");
  const acpHarness = executionBackend === "opencode" || executionBackend === "kimi" ? executionBackend : null;
  const backend = executionBackend === "windows" || executionBackend === "wsl" || executionBackend === "docker"
    ? executionBackend : "api";
  const [dockerAvailable, setDockerAvailable] = useState(false);
  const [environmentPreferences, setEnvironmentPreferences] = useState<(EnvironmentPreferences & { cid: number }) | null>(null);
  const [nativeModelChoices, setNativeModelChoices] = useState<NativeModelChoices>(defaultNativeChoices);
  const [nativeBusy, setNativeBusy] = useState(false);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [providers, setProviders] = useState<Record<string, { base_url: string }>>({});
  const [convs, setConvs] = useState<Conv[]>([]);
  const [loadedConversationScope, setLoadedConversationScope] = useState<string | null>(null);
  const [conversationLoadError, setConversationLoadError] = useState("");
  const [convId, setConvId] = useState<number | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [toolCards, setToolCards] = useState<ToolCard[]>([]);
  const [pendingTools, setPendingTools] = useState<PendingTool[]>([]);
  const decidingConversations = useRef(new Set<number>());
  const [approvalBusy, setApprovalBusy] = useState<ReadonlySet<number>>(new Set());
  const [goal, setGoal] = useState<Goal | null>(null);
  const [plan, setPlan] = useState<PlanStep[] | null>(null);
  const draft = useStore(currentAgentDraft);
  const { input, images: attachments } = draft;
  const [voiceStatus, setVoiceStatus] = useState<VoiceState>("stopped");
  const [voiceCapabilities, setVoiceCapabilities] = useState<{server_transcription:boolean;max_seconds:number} | null>(null);
  const voiceRef = useRef<VoiceSession | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const importInputRef = useRef<HTMLInputElement>(null);
  const importNoticeRef = useRef<{ cid: number; text: string } | null>(null);
  const updateInput = (value: string | ((previous: string) => string)) => {
    useStore.getState().updateAgentDraft(current => ({
      ...current, input: typeof value === "function" ? value(current.input) : value,
    }));
  };
  const [streaming, setStreaming] = useState(false);
  const [modelUsage, setModelUsage] = useState<{key: string; value?: ModelUsage} | null>(null);
  const [compressingId, setCompressingId] = useState<number | null>(null);
  const [runTimings, setRunTimings] = useState<Record<string, RunTiming>>({});
  const activeRunRef = useRef<string | null>(null);
  const toggleVoice = () => {
    if (voiceRef.current) { voiceRef.current.stop(); return; }
    if (!caseId || !convId) return;
    const project = captureProjectCase();
    const target = convId;
    const onText = (text: string) => {
      if (project?.isCurrent() && convIdRef.current === target) {
        updateInput(previous => `${previous.trim()} ${text}`.trim());
      }
    };
    const onState = (state: VoiceState) => {
      if (voiceRef.current !== controller) return;
      setVoiceStatus(state);
      if (state === "stopped" || state.startsWith("error:")) voiceRef.current = null;
    };
    let controller: VoiceSession;
    if (voiceCapabilities?.server_transcription) {
      const locale = navigator.language.slice(0,2).toLowerCase();
      const language = locale === "zh" || locale === "en" ? locale : "auto";
      controller = new RecordedVoice(voiceCapabilities.max_seconds, async (audio, signal) => {
        const response = await api.putBytes<{text:string}>(`/cases/${caseId}/voice/transcription?language=${language}`, audio, signal);
        return response.text;
      }, onText, onState);
    } else {
      const w = window as any;
      const Recognition = w.SpeechRecognition ?? w.webkitSpeechRecognition;
      if (!Recognition) { setVoiceStatus("error:当前浏览器不支持语音识别，请输入文字。"); return; }
      controller = new VoiceController(() => new Recognition(), onText, onState);
    }
    voiceRef.current = controller;
    controller.start();
  };
  useEffect(() => {
    if (!userId) return;
    const request = new AbortController();
    setVoiceCapabilities(null);
    api.get<{server_transcription:boolean;max_seconds:number}>("/voice/capabilities", undefined, request.signal)
      .then(result => { if (!request.signal.aborted) setVoiceCapabilities(result); })
      .catch(() => {});
    return () => request.abort();
  }, [userId]);
  useEffect(() => {
    setVoiceStatus("stopped");
    return () => {
      const voice = voiceRef.current;
      voiceRef.current = null;
      voice?.cancel();
    };
  }, [conversationScope, convId]);
  const [loadingConv, setLoadingConv] = useState(false);
  const [olderPage, setOlderPage] = useState<string | null>(null);
  const historyGeneration = useRef(0);
  const historyPending = useRef(false);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [olderLoading, setOlderLoading] = useState(false);
  const [streamText, setStreamText] = useState("");
  const streamTextRef = useRef("");
  const [streamReasoning, setStreamReasoning] = useState("");
  const streamReasoningRef = useRef("");
  const convIdRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);
  const [reasoningEffort, setReasoningEffort] = useState("off");
  const [soundEnabled, setSoundEnabled] = useState(() => replySoundEnabled(userId));
  const [showNewProfile, setShowNewProfile] = useState(false);
  const [editProfile, setEditProfile] = useState(false);
  const [ep, setEp] = useState({ provider: "", name: "", base_url: "", default_model: "", api_key: "" });
  const [modelOptions, setModelOptions] = useState<{ id: string }[]>([]);
  const [profileStatus, setProfileStatus] = useState("");
  const [convModel, setConvModel] = useState("");
  const [profileId, setProfileId] = useState<number | null>(null);
  const [permMode, setPermMode] = useState("approve_writes");
  const [savingSettingsId, setSavingSettingsId] = useState<number | null>(null);
  const [skills, setSkills] = useState<{ name: string; description: string;
    display_name?: string; short_description?: string; default_prompt?: string }[]>([]);
  const [skillQuery, setSkillQuery] = useState<string | null>(null);
  const matchingSkills = skillQuery === null ? [] :
    skills.filter((skill) => skill.name.toLowerCase().includes(skillQuery.toLowerCase())).slice(0, 8);
  const [fileQuery, setFileQuery] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<{ message: Msg; ok: boolean } | null>(null);
  const [highlightMessage, setHighlightMessage] = useState<Msg | null>(null);
  const [highlightTool, setHighlightTool] = useState<string | ToolCardState | null>(null);
  const [changesOpen, setChangesOpen] = useState(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const followsLatest = useRef(true);
  const historyAnchor = useRef<{ element: Element; top: number } | null>(null);

  const [np, setNp] = useState({ provider: "custom", name: "", base_url: "", default_model: "", api_key: "" });

  useEffect(() => { setSoundEnabled(replySoundEnabled(userId)); }, [userId]);

  useEffect(() => {
    let active = true;
    const reloadProfiles = () => {
      api.get<Profile[]>("/provider-profiles").then((items) => {
        if (!active) return;
        setProfiles(items);
        setProfileId((current) => current ?? items[0]?.id ?? null);
      }).catch(() => {});
    };
    reloadProfiles();
    window.addEventListener(providerProfilesChanged, reloadProfiles);
    api.get<Record<string, { base_url: string }>>("/llm/providers").then(setProviders).catch(() => {});
    api.get<{ skills?: { name: string; description: string }[] } | { name: string; description: string }[]>(
      "/skills").then((r) => setSkills(Array.isArray(r) ? r : r.skills ?? [])).catch(() => {});
    return () => { active = false; window.removeEventListener(providerProfilesChanged, reloadProfiles); };
  }, []);

  useEffect(() => {
    const current = profiles.find((profile) => profile.id === profileId);
    if (!current) return;
    setEp({ provider: current.provider, name: current.name, base_url: current.base_url,
            default_model: current.default_model, api_key: "" });
    setModelOptions([]);
    setProfileStatus("");
  }, [profileId]);

  useEffect(() => {
    let current = true;
    setConvs([]); setConvId(null); convIdRef.current = null;
    setLoadedConversationScope(null); setConversationLoadError("");
    if (!caseId || !conversationScope) return;
    api.get<Conv[]>(`/conversations?caseId=${caseId}`).then((cs) => {
      if (!current) return;
      setConvs(cs);
      const previous = SELECTED_CONVERSATIONS.get(conversationScope);
      setConvId(cs.find(item => item.id === previous)?.id ?? cs[0]?.id ?? null);
      setLoadedConversationScope(conversationScope);
    }).catch(() => { if (current) setConversationLoadError(conversationScope); });
    return () => { current = false; };
  }, [caseId, conversationScope]);

  useEffect(() => { convIdRef.current = convId; }, [convId]);
  useEffect(() => {
    setDockerAvailable(false);
    if (!userId) return;
    const controller = new AbortController();
    api.get<{docker: boolean}>("/codex/capabilities", undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDockerAvailable(value.docker); }).catch(() => {});
    return () => controller.abort();
  }, [userId]);
  useEffect(() => {
    if (conversationScope && loadedConversationScope === conversationScope && convId) {
      SELECTED_CONVERSATIONS.set(conversationScope, convId);
    }
  }, [conversationScope, loadedConversationScope, convId]);

  useEffect(() => {
    if (!convId || !user) return;
    const preference = localStorage.getItem(`vf-chat-backend:${user.id}:${convId}`);
    setBackend(preference === "windows" || preference === "wsl" || preference === "docker" ||
      preference === "opencode" || preference === "kimi" ? preference : "api");
    let current = true;
    api.get(`/codex/conversations/${convId}/activity`).then(live => {
      const activeRuntime = live.active_turn?.runtime ?? live.active_fork?.runtime ?? live.active_session_request?.runtime ?? (!preference ? live.native_fork?.runtime : null);
      if (current && (activeRuntime === "windows" || activeRuntime === "wsl" || activeRuntime === "docker")) setBackend(activeRuntime);
    }).catch(() => {});
    return () => { current = false; };
  }, [convId, user?.id]);

  useEffect(() => {
    if (!convId || !userId) return;
    const controller = new AbortController();
    const read = () => api.get<EnvironmentPreferences>(`/conversations/${convId}/model-preferences`, undefined, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setEnvironmentPreferences({ ...value, cid: convId }); })
      .catch(() => {});
    void read();
    window.addEventListener(providerProfilesChanged, read);
    return () => { controller.abort(); window.removeEventListener(providerProfilesChanged, read); };
  }, [convId, userId]);

  // 加载会话：消息 + 摘要 + 工具结果 + 待审批；后台运行按 cid 独立恢复投影。
  useEffect(() => {
    const generation = ++historyGeneration.current;
    let stale = false;
    const selected = loadedConversationScope === conversationScope ? convId : null;
    historyPending.current = Boolean(selected);
    setOlderLoading(false);
    const background = selected ? ACTIVE_RUNS.get(runKey(conversationScope, selected)) : undefined;
    streamTextRef.current = background?.text ?? "";
    streamReasoningRef.current = background?.reasoning ?? "";
    cancelledRef.current = background?.cancelled ?? false;
    activeRunRef.current = background?.runId ?? null;
    setStreamText(streamTextRef.current);
    setStreamReasoning(streamReasoningRef.current);
    setStreaming(!!background);
    setModelUsage(background ? {key: background.key, value: background.usage} : null);
    setProfileStatus("");
    setCopyState(null);
    setHighlightMessage(null);
    setHighlightTool(null);
    followsLatest.current = true;
    historyAnchor.current = null;
    setGoal(null);
    setPlan(null);
    if (!selected) { setMsgs([]); setToolCards([]); setPendingTools([]); setRunTimings({}); setLoadingConv(false); return; }
    setLoadingConv(true);
    setOlderPage(null);
    api.get(`/conversations/${selected}`).then((c) => {
      if (stale || historyGeneration.current !== generation || convIdRef.current !== selected) return;
      setProfileId(c.provider_profile_id ?? null);
      setConvModel(c.model ?? "");
      setPermMode(c.permission_mode ?? "approve_writes");
      const projected = projectConversationEvents(c.events, c.tool_calls ?? []);
      const latest = ACTIVE_RUNS.get(runKey(conversationScope, selected));
      setModelUsage({key: runKey(conversationScope, selected), value: latest ? latest.usage : c.latest_model_usage});
      setMsgs(projected.messages);
      setToolCards(projected.tools);
      setRunTimings(projected.runTimings);
      setOlderPage(olderConversationPage(selected, c.event_window));
      setPendingTools(c.pending_tools ?? []);
      const restoredGoal: Goal | null = c.current_goal ?? null;
      setGoal((previous) => previous && (!restoredGoal || (previous.goal_id === restoredGoal.goal_id
        && previous.revision > restoredGoal.revision)) ? previous : restoredGoal);
      const pastPlans = projected.tools.filter((card) => card.tool === "update_plan" && card.ok && card.status === "completed"
        && card.result.startsWith("计划已更新:\n"));
      setPlan((previous) => previous ?? planSteps(c.current_plan) ?? planSteps(pastPlans.at(-1)?.args?.steps));
      if (importNoticeRef.current?.cid === selected) {
        setProfileStatus(importNoticeRef.current.text);
        importNoticeRef.current = null;
      }
      streamTextRef.current = latest?.text ?? "";
      streamReasoningRef.current = latest?.reasoning ?? "";
      cancelledRef.current = latest?.cancelled ?? false;
      activeRunRef.current = latest?.runId ?? null;
      setStreamText(streamTextRef.current);
      setStreamReasoning(streamReasoningRef.current);
      setStreaming(!!latest);
    }).catch(() => {}).finally(() => {
      if (!stale && historyGeneration.current === generation && convIdRef.current === selected) {
        historyPending.current = false;
        setLoadingConv(false);
      }
    });
    return () => { stale = true; };
  }, [convId, executionBackend, conversationScope, loadedConversationScope, historyRefresh]);

  useLayoutEffect(() => {
    const list = messageListRef.current;
    const anchor = historyAnchor.current;
    historyAnchor.current = null;
    if (!list) return;
    if (anchor && list.contains(anchor.element)) {
      list.scrollTop += anchor.element.getBoundingClientRect().top - anchor.top;
    } else if (followsLatest.current) {
      list.scrollTop = list.scrollHeight;
    }
  }, [msgs, streamText, streamReasoning, pendingTools, toolCards]);

  const loadOlder = async () => {
    if (!olderPage || !convId || olderLoading) return;
    const generation = historyGeneration.current;
    setOlderLoading(true);
    try {
      const result = await api.get(olderPage);
      if (historyGeneration.current !== generation || convIdRef.current !== convId) return;
      const projected = projectConversationEvents(result.events, result.tool_calls ?? []);
      const list = messageListRef.current;
      if (list) {
        const top = list.firstElementChild?.getBoundingClientRect().bottom ?? list.getBoundingClientRect().top;
        const entries = list.querySelectorAll('[data-testid="chat-message"], [data-testid="chat-tool-card"]');
        const element = Array.from(entries).find((entry) => entry.getBoundingClientRect().bottom > top);
        if (element) historyAnchor.current = { element, top: element.getBoundingClientRect().top };
      }
      followsLatest.current = false;
      setMsgs((current) => [...projected.messages, ...current]);
      setToolCards((current) => {
        const existing = new Set(current.map((card) => card.id));
        return [...projected.tools.filter((card) => !card.id || !existing.has(card.id)), ...current];
      });
      setRunTimings((current) => ({...projected.runTimings, ...current}));
      setOlderPage(olderConversationPage(convId, result.event_window));
    } catch (error) {
      if (historyGeneration.current === generation) setProfileStatus(`加载历史失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      if (historyGeneration.current === generation) setOlderLoading(false);
    }
  };

  const createProfile = async () => {
    await api.post("/provider-profiles", np);
    setShowNewProfile(false);
    const p = await api.get<Profile[]>("/provider-profiles");
    setProfiles(p);
    setProfileId(p[p.length - 1].id);
  };

  const loadModelOptions = async () => {
    if (!profileId) return;
    try {
      const result = await api.get<{ models: { id: string }[]; fallback: boolean; error?: string }>(
        `/llm/models?provider_profile_id=${profileId}`);
      setModelOptions(result.models);
      setProfileStatus(result.fallback ? `模型列表回退：${result.error}` : `已获取 ${result.models.length} 个模型`);
    } catch (error: any) {
      setProfileStatus(`模型列表获取失败：${error?.message ?? String(error)}`);
    }
  };

  const saveEditedProfile = async () => {
    if (!profileId || streaming || nativeBusy) return;
    try {
      const payload: Record<string, string> = { ...ep };
      if (!payload.api_key) delete payload.api_key;
      const updated = await api.patch<Profile>(`/provider-profiles/${profileId}`, payload);
      const list = await api.get<Profile[]>("/provider-profiles");
      setProfiles(list);
      setProfileId(updated.id);
      setEp({ provider: updated.provider, name: updated.name, base_url: updated.base_url,
              default_model: updated.default_model, api_key: "" });
      setConvModel(updated.default_model);
      setEditProfile(false);
      window.dispatchEvent(new Event(providerProfilesChanged));
      setProfileStatus("profile 已保存");
    } catch (error: any) {
      setProfileStatus(`保存失败（已保留编辑内容）：${error?.message ?? String(error)}`);
    }
  };

  const testProfile = async () => {
    if (!profileId) return;
    try {
      const result = await api.post<{ model_count: number; latency_ms: number }>(
        `/provider-profiles/${profileId}:test`, {});
      setProfileStatus(`连接成功：${result.model_count} 个模型 · ${result.latency_ms} ms`);
    } catch (error: any) {
      setProfileStatus(`连接测试失败：${error?.message ?? String(error)}`);
    }
  };

  const newConv = async () => {
    if (!currentCase || !profileId) return;
    const c = await api.post<Conv>("/conversations", {
      case_id: currentCase.id, title: `会话 ${convs.length + 1}`,
      provider_profile_id: profileId,
      model: convModel || profiles.find((p) => p.id === profileId)?.default_model || "",
      permission_mode: permMode,
    });
    setConvs([c, ...convs]);
    setConvId(c.id);
    setMsgs([]); setToolCards([]); setPendingTools([]); setGoal(null); setPlan(null);
  };

  /** 分叉当前会话（零拷贝：后端记分支头指针，历史递归合并） */
  const forkConv = async () => {
    if (!convId || streaming) return;
    const c = await api.post<Conv>(`/conversations/${convId}/fork`, {});
    setConvs([c, ...convs]);
    setConvId(c.id);
  };

  const forkFromMessage = async (index: number) => {
    if (!convId || streaming) return;
    const message = msgs[index];
    if (!message || message.role !== "assistant" || message.canFork === false || message.cancelled || message.interrupted || message.tool_calls?.length) return;
    try {
      let eventId = message.id;
      if (!eventId && message.run_id) {
        const detail = await api.get(`/conversations/${convId}`);
        const matches = detail.events.filter((event: any) =>
          event.type === "message" && event.payload.role === "assistant" &&
          event.payload.run_id === message.run_id && !event.payload.tool_calls?.length &&
          !event.payload.cancelled && event.can_fork !== false);
        if (matches.length === 1) eventId = matches[0].id;
      }
      if (!eventId) throw new Error("找不到已完成回答的事件边界");
      if (convIdRef.current !== convId) return;
      const branch = await api.post<Conv>(`/conversations/${convId}/fork`, {head_event_id: eventId});
      if (convIdRef.current !== convId) return;
      setConvs((items) => [branch, ...items]);
      setConvId(branch.id);
    } catch (error: any) {
      setProfileStatus(`分支失败：${error?.message ?? String(error)}`);
    }
  };

  /** 压缩当前会话上下文（LLM 摘要落库，后续对话基于摘要续跑） */
  const compressConv = async () => {
    if (!convId || !profileId || streaming || compressingId !== null) return;
    const sourceId = convId;
    setCompressingId(sourceId);
    try {
      const result = await api.post(`/conversations/${sourceId}/compress`,
        { provider_profile_id: profileId, model: convModel, keep_last: 6 });
      if (convIdRef.current !== sourceId) return;
      if (result.ok) setHistoryRefresh(value => value + 1);
      else setProfileStatus(result.message);
    } catch (e: any) {
      if (convIdRef.current === sourceId) setProfileStatus(`压缩失败：${e?.message ?? String(e)}`);
    } finally {
      setCompressingId(null);
    }
  };

  /** 以最近压缩摘要或有界文本开启新会话，不复制完整历史。 */
  const handoffConv = async () => {
    if (!convId || streaming || compressingId === convId) return;
    const sourceId = convId;
    try {
      const handoff = await api.post<Conv>(`/conversations/${sourceId}/handoff`, { keep_chars: 180000 });
      if (convIdRef.current !== sourceId) return;
      setConvs(items => [handoff, ...items]);
      setConvId(handoff.id);
    } catch (e: any) {
      if (convIdRef.current === sourceId) setProfileStatus(`交接失败：${e?.message ?? String(e)}`);
    }
  };

  const renameConv = async () => {
    if (!convId || streaming) return;
    const current = convs.find((conv) => conv.id === convId);
    const title = window.prompt("重命名会话", current?.title ?? "");
    if (title === null || !title.trim()) return;
    try {
      const updated = await api.patch<Conv>(`/conversations/${convId}`, {title: title.trim()});
      setConvs((items) => items.map((item) => item.id === convId ? updated : item));
    } catch (error: any) {
      setProfileStatus(`重命名失败：${error?.message ?? String(error)}`);
    }
  };

  const approvePlan = async () => {
    if (!convId || streaming || nativeBusy) return;
    try {
      const updated = await api.patch<Conv>(`/conversations/${convId}/permission`,
        {permission_mode: "approve_writes"});
      setConvs((items) => items.map((item) => item.id === convId ? updated : item));
      if (convIdRef.current !== convId) return;
      setPermMode(updated.permission_mode ?? "approve_writes");
      setProfileStatus(backend === "api" ? "Plan 已批准。后续写入仍需逐次审批。"
        : backend === "docker" ? "Plan 已批准。Codex 可编辑算例副本，回合结束后核对版本并发布。"
          : "Plan 已批准。Codex 可以写入工作区，超出沙箱的操作需审批。");
    } catch (error: any) {
      setProfileStatus(`批准 Plan 失败：${error?.message ?? String(error)}`);
    }
  };

  const saveSessionSettings = async () => {
    if (!convId || !profileId || !settingsChanged || settingsBusy || savingSettingsId !== null
      || pendingTools.length > 0 || compressingId === convId) return;
    const target = convId;
    setSavingSettingsId(target);
    try {
      const saved = await api.patch<Conv & { model_preference: ModelPreference }>(`/conversations/${target}/settings`, {
        provider_profile_id: profileId, model: convModel.trim(), permission_mode: permMode,
        runtime_preferences: { runtime: backend, ...(backend === "api" ? { reasoning_effort: reasoningEffort } : {
          native_options: Object.fromEntries(Object.entries(nativeModelChoices).filter(([, value]) => value !== "")),
        }) },
      });
      setConvs(items => items.map(item => item.id === target ? saved : item));
      if (convIdRef.current === target) {
        setEnvironmentPreferences((existing) => ({ cid: target,
          preferences: { ...(existing?.cid === target ? existing.preferences : {}), [backend]: saved.model_preference },
          stale: existing?.cid === target ? existing.stale.filter((runtime) => runtime !== backend) : [],
        }));
      }
      if (convIdRef.current === target) {
        setConvModel(saved.model);
        setProfileStatus("已保存当前会话设置");
      }
    } catch (error) {
      if (convIdRef.current === target) {
        setProfileStatus(`会话设置未保存。${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      setSavingSettingsId(null);
    }
  };

  const deleteConv = async () => {
    if (!convId || streaming || pendingTools.length) return;
    if (!window.confirm("删除当前会话及其事件/工具记录？算例文件不会删除。")) return;
    try {
      await api.del(`/conversations/${convId}`);
      const remaining = convs.filter((conv) => conv.id !== convId);
      setConvs(remaining);
      setConvId(remaining[0]?.id ?? null);
    } catch (error: any) {
      setProfileStatus(`删除失败：${error?.message ?? String(error)}`);
    }
  };

  const exportConv = async () => {
    if (!convId || streaming || pendingTools.length) return;
    try {
      const token = localStorage.getItem("vf_token");
      const response = await fetch(`/api/v1/conversations/${convId}/export`, {
        headers: {...(token ? { Authorization: `Bearer ${token}` } : {})},
      });
      if (!response.ok) throw new Error(await response.text());
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `conversation-${convId}.jsonl`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error: any) {
      setProfileStatus(`导出失败：${error?.message ?? String(error)}`);
    }
  };

  const importConv = async (file: File | undefined) => {
    if (!file || !currentCase || streaming) return;
    if (file.size > 16 * 1024 * 1024) {
      setProfileStatus("导入失败：文件超过 16 MiB");
      return;
    }
    try {
      const token = localStorage.getItem("vf_token");
      const response = await fetch(`/api/v1/conversations:import?case_id=${currentCase.id}`, {
        method: "POST", headers: {...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/x-ndjson"},
        body: await file.text(),
      });
      if (!response.ok) throw new Error(await response.text());
      const imported = await response.json() as Conv & { imported_events: number };
      const list = await api.get<Conv[]>(`/conversations?caseId=${currentCase.id}`);
      setConvs(list);
      importNoticeRef.current = {cid: imported.id,
        text: `已导入 ${imported.imported_events} 条历史事件；活动任务不会自动恢复`};
      setConvId(imported.id);
    } catch (error: any) {
      setProfileStatus(`导入失败：${error?.message ?? String(error)}`);
    } finally {
      if (importInputRef.current) importInputRef.current.value = "";
    }
  };

  const beginRun = (cid: number) => {
    prepareReplyAudio(userId);
    const run: ActiveRun = {key: runKey(conversationScope, cid), controller: new AbortController(), text: "", reasoning: "",
      cancelled: false, runId: null};
    ACTIVE_RUNS.set(run.key, run);
    if (convIdRef.current === cid) {
      streamTextRef.current = "";
      streamReasoningRef.current = "";
      cancelledRef.current = false;
      activeRunRef.current = null;
      setStreamText("");
      setStreamReasoning("");
      setStreaming(true);
      setModelUsage({key: run.key});
    }
    return run;
  };

  const finishRun = (run: ActiveRun) => {
    if (ACTIVE_RUNS.get(run.key) !== run) return;
    ACTIVE_RUNS.delete(run.key);
    notifyRun(run, {kind: "finished"});
  };

  const runGoal = async () => {
    if (!convId || !goal || goal.status !== "active" || ACTIVE_RUNS.has(runKey(conversationScope, convId))) return;
    const runCid = convId;
    const run = beginRun(runCid);
    try {
      const token = localStorage.getItem("vf_token");
      const response = await fetch(`/api/v1/conversations/${runCid}/goal:run`, {
        method: "POST", signal: run.controller.signal,
        headers: {...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/json"}, body: "{}",
      });
      if (!response.ok) throw new Error(await response.text());
      await consumeSSE(response, (ev) => receiveRunFrame(run, ev, userId));
    } catch (error: any) {
      if (!run.controller.signal.aborted && ACTIVE_RUNS.get(run.key) === run) {
        notifyRun(run, {kind: "error", status: `目标执行失败：${error?.message ?? String(error)}`});
      }
    } finally {
      finishRun(run);
    }
  };

  const appendMessage = (message: Msg) => {
    setMsgs(current => message.id !== undefined && current.some(row => row.id === message.id)
      ? current : [...current, message]);
  };

  const flushStream = (id?: number, canFork = false, interrupted = false) => {
    const text = streamTextRef.current;
    const reasoning = streamReasoningRef.current;
    const cancelled = cancelledRef.current && !interrupted;
    const candidate: Msg = { id, canFork, role: "assistant", content: text, reasoning, cancelled, interrupted,
                             run_id: activeRunRef.current ?? undefined };
    if (filterDisplayMessages([candidate]).length) {
      appendMessage(candidate);
    }
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    cancelledRef.current = false;
    setStreamText("");
    setStreamReasoning("");
  };

  const reconcilePendingHistory = () => {
    if (!historyPending.current) return;
    // An outstanding snapshot may predate the event delivered to this view.
    historyPending.current = false;
    historyGeneration.current++;
    setHistoryRefresh(value => value + 1);
  };

  // Only the mounted view for this account and conversation applies live updates.
  const applyRunNotice = useEffectEvent((run: ActiveRun, notice: RunNotice) => {
    if (!convIdRef.current || run.key !== runKey(conversationScope, convIdRef.current)) return;
    if (notice.kind === "accepted") { appendMessage(notice.message); return; }
    if (notice.kind === "error") {
      setProfileStatus(notice.status);
      streamTextRef.current = run.text;
      setStreamText(run.text);
      return;
    }
    if (notice.kind === "finished") {
      setStreaming(false);
      flushStream(undefined, false, run.controller.signal.aborted);
      if (run.cancelled) {
        setProfileStatus(run.controller.signal.aborted ? "已断开当前会话的输出连接" : "当前会话已停止");
      }
      reconcilePendingHistory();
      return;
    }
    const ev = notice.event;
    if (ev.type === "request.usage") {
      setModelUsage({key: run.key, value: run.usage});
      return;
    }
    if (ev.type === "ready" && ev.run_id) {
      activeRunRef.current = ev.run_id;
      const startedAt = Number.isFinite(ev.started_at) ? ev.started_at * 1000 : Date.now();
      setRunTimings((current) => ({...current, [ev.run_id]: {
        runId: ev.run_id, status: "started", startedAt,
      }}));
    }
    const terminalStatus = ({
      "run.completed": "completed", "run.cancelled": "cancelled",
      "request.failed": "failed", "request.timed_out": "timed_out", "stream.error": "failed",
    } as Record<string, string>)[ev.type];
    if (terminalStatus && ev.run_id) {
      const endedAt = Date.now();
      setRunTimings((current) => {
        const previous = current[ev.run_id];
        const startedAt = previous?.startedAt ?? endedAt;
        return {...current, [ev.run_id]: {runId: ev.run_id, status: terminalStatus,
          startedAt, endedAt, durationMs: Math.max(0, endedAt - startedAt)}};
      });
    }
    if (ev.type === "text.delta" || ev.type === "request.failed" || ev.type === "stream.error") {
      streamTextRef.current = run.text;
      setStreamText(run.text);
    } else if (ev.type === "reasoning.delta") {
      streamReasoningRef.current = run.reasoning;
      setStreamReasoning(run.reasoning);
    } else if (ev.type === "tool.state") {
      setToolCards((cs) => upsertToolCard(cs, { id: ev.tool_call_id, tool: ev.tool,
        ok: false, result: "", status: ev.status, args: ev.args }));
    }
    else if (ev.type === "tool.result") {
      setPendingTools((pt) => pt.filter((tool) => toolCallKey(tool.id) !== toolCallKey(ev.tool_call_id)));
      setToolCards((cs) => upsertToolCard(cs, { id: ev.tool_call_id, tool: ev.tool, ok: ev.ok,
        result: ev.result, status: ev.status ?? (ev.ok ? "completed" : "error"),
        approved: ev.approved, auto: ev.auto, rejected: ev.rejected, args: ev.args }));
      if (ev.tool === "update_plan" && ev.ok && (!ev.status || ev.status === "completed")) {
        const steps = planSteps(ev.args?.steps);
        if (steps) setPlan(steps);
      }
    } else if (ev.type === "approval.requested") {
      setPendingTools((pt) => pt.some((tool) => tool.id === ev.tool_call.id) ? pt : [...pt, ev.tool_call]);
      setToolCards((cs) => upsertToolCard(cs, { id: ev.tool_call.id, tool: ev.tool_call.tool,
        ok: false, result: "", status: "awaiting_approval", args: ev.tool_call.args }));
    } else if (ev.type === "goal.updated") {
      const updated: Goal = ev.goal;
      setGoal((previous) => previous?.goal_id === updated.goal_id && previous.revision > updated.revision ? previous : updated);
    } else if (ev.type === "goal.progress") {
      setProfileStatus(`目标执行：第 ${ev.turn}/${ev.max_turns} 轮 · 工具 ${ev.tool_calls_used}/${ev.max_tool_calls}`);
    } else if (ev.type === "run.cancelled") {
      cancelledRef.current = true;
    } else if (ev.type === "run.completed") {
      activeRunRef.current = run.runId;
      streamTextRef.current = run.text;
      streamReasoningRef.current = run.reasoning;
      flushStream(ev.answer_event_id, true);
      reconcilePendingHistory();
    }
  });

  useEffect(() => {
    if (!conversationScope || !convId || loadedConversationScope !== conversationScope) return;
    const key = runKey(conversationScope, convId);
    const listener = (run: ActiveRun, notice: RunNotice) => applyRunNotice(run, notice);
    RUN_VIEWS.set(key, listener);
    return () => { if (RUN_VIEWS.get(key) === listener) RUN_VIEWS.delete(key); };
  }, [conversationScope, loadedConversationScope, convId]);

  const addImages = async (files: File[]) => {
    const project = captureProjectCase();
    for (const file of files.slice(0, Math.max(0, maxMessageImages - attachments.length))) {
      if (!project?.isCurrent()) return;
      try {
        const images = await readImageFiles([file], currentAgentDraft(useStore.getState()).images.length);
        if (!project.isCurrent()) return;
        useStore.getState().updateAgentDraft(current => {
          if (current.images.length >= maxMessageImages) throw new Error("每条消息最多 3 张图片");
          return { ...current, images: [...current.images, ...images] };
        });
      } catch (error) {
        if (project.isCurrent()) setProfileStatus(error instanceof Error ? error.message : String(error));
      }
    }
  };

  const send = async () => {
    if ((!input.trim() && !attachments.length) || !convId || !currentConv || !profileId || streaming
        || loadingConv || compressingId === convId || savingSettingsId === convId) return;
    const project = captureProjectCase();
    if (!project) return;
    const runCid = convId;
    // @路径附件：把算例内文件内容内联进消息（原版 addPathToChat 语义）
    let content = input.trim();
    if (currentCase) {
      const refs = parsePathReferences(content)
        .filter((ref) => useStore.getState().fileTree.some((t) => t.path === ref.path));
      const invalid = refs.find((ref) => ref.error);
      if (invalid) {
        setStreamText(`[附件错误] ${invalid.path}: ${invalid.error}`);
        return;
      }
      if (refs.length) {
        const blocks: string[] = [];
        for (const ref of refs.slice(0, 5)) {          // 最多带 5 个附件
          try {
            const f = await api.get<{ content?: string; kind: string }>(
              `/cases/${currentCase.id}/file?path=${encodeURIComponent(ref.path)}`);
            if (f.kind === "text" && f.content !== undefined) {
              blocks.push(formatAttachment(f.content, ref));
            }
          } catch (error) {
            setStreamText(`[附件错误] ${ref.path}: ${error instanceof Error ? error.message : String(error)}`);
            return;
          }
        }
        content += blocks.join("");
      }
    }
    if (!project.isCurrent() || convIdRef.current !== runCid || ACTIVE_RUNS.has(runKey(conversationScope, runCid))) return;
    const run = beginRun(runCid);
    try {
      const token = localStorage.getItem("vf_token");
      const res = await fetch(`/api/v1/conversations/${runCid}/messages`, {
        method: "POST",
        signal: run.controller.signal,
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ content, provider_profile_id: profileId,
                               model: convModel || profiles.find((p) => p.id === profileId)?.default_model || "",
                               reasoning_effort: reasoningEffort, images: attachments }),
      });
      if (!res.ok) throw new Error(await res.text());
      clearSubmittedAgentDraft(project, draft);
      const eventId = Number(res.headers.get("X-VibeFlow-Message-Event"));
      if (ACTIVE_RUNS.get(run.key) === run) {
        notifyRun(run, {kind: "accepted", message: {role: "user", content, images: attachments,
          id: Number.isSafeInteger(eventId) && eventId > 0 ? eventId : undefined}});
      }
      await consumeSSE(res, (ev) => receiveRunFrame(run, ev, userId));
    } catch (error: any) {
      if (!run.controller.signal.aborted && ACTIVE_RUNS.get(run.key) === run) {
        const message = error?.message ?? String(error);
        run.text += `\n\n[发送失败] ${message}`;
        notifyRun(run, {kind: "error", status: `发送失败：${message}`});
      }
    } finally {
      finishRun(run);
    }
  };

  const decide = async (tc: PendingTool, approve: boolean) => {
    const project = captureProjectCase();
    if (!convId || !project || loadingConv || convIdRef.current !== convId ||
        ACTIVE_RUNS.has(runKey(conversationScope, convId)) || decidingConversations.current.has(convId)) return;
    const runCid = convId;
    const generation = historyGeneration.current;
    const selected = () => project.isCurrent() && convIdRef.current === runCid && historyGeneration.current === generation;
    const path = `/conversations/${runCid}/tool-calls/${tc.id}`;
    const decisionSet = decidingConversations.current;
    decisionSet.add(runCid);
    setApprovalBusy(new Set(decisionSet));
    setProfileStatus("正在提交审批…");
    const run = approve ? beginRun(runCid) : undefined;
    try {
      if (!run) {
        await api.post(`${path}:reject`, {});
        if (selected()) {
          setPendingTools((pt) => pt.filter((tool) => tool.id !== tc.id));
          setToolCards((cs) => upsertToolCard(cs, { id: tc.id, tool: tc.tool, ok: false,
            result: "用户拒绝了该操作", status: "cancelled", rejected: true, args: tc.args }));
          setProfileStatus("");
        }
        return;
      }
      const token = localStorage.getItem("vf_token");
      const res = await fetch(`/api/v1${path}:approve`, {
        method: "POST", signal: run.controller.signal, headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/json" },
        body: JSON.stringify({ reasoning_effort: reasoningEffort }),
      });
      if (!res.ok) throw new Error(await res.text());
      let receipt = false;
      await consumeSSE(res, (ev) => {
        if (ev.type === "tool.result" && toolCallKey(ev.tool_call_id) === toolCallKey(tc.id)) receipt = true;
        receiveRunFrame(run, ev, userId);
      });
      if (!receipt) throw new Error("未收到工具结果");
      if (selected()) setProfileStatus((status) => status === "正在提交审批…" ? "" : status);
    } catch {
      if (!selected()) return;
      try {
        const history = await api.get(`/conversations/${runCid}`);
        if (!selected()) return;
        const pending = (history.pending_tools as PendingTool[]).find((tool) => tool.id === tc.id);
        const recorded = projectToolCards(history.events, history.tool_calls)
          .find((card) => card.id === toolCallKey(tc.id));
        setPendingTools((pt) => {
          if (!pending) return pt.filter((tool) => tool.id !== tc.id);
          if (pt.some((tool) => tool.id === tc.id)) return pt;
          return [...pt, pending];
        });
        if (recorded) setToolCards((cs) => upsertToolCard(cs, recorded));
        setProfileStatus(pending ? "审批仍在等待，可以重试。" : "已同步服务器的审批状态。");
      } catch {
        if (selected()) setProfileStatus("暂时无法确认审批状态，请刷新会话后查看。");
      }
    } finally {
      if (run) finishRun(run);
      decisionSet.delete(runCid);
      setApprovalBusy(new Set(decisionSet));
    }
  };

  const stopRun = async () => {
    if (!streaming || !convId) return;
    const runCid = convId;
    const run = ACTIVE_RUNS.get(runKey(conversationScope, runCid));
    if (!run || run.cancelled) return;
    run.cancelled = true;
    cancelledRef.current = true;
    setProfileStatus("正在停止当前会话…");
    const request = new AbortController();
    window.setTimeout(() => {
      request.abort();
      if (ACTIVE_RUNS.get(run.key) === run) run.controller.abort();
    }, 2000);
    try {
      const result = await api.post<{ ok?: boolean }>(`/conversations/${runCid}/messages:cancel`,
        {run_id: run.runId}, undefined, request.signal);
      if (result?.ok === false && ACTIVE_RUNS.get(run.key) === run) {
        notifyRun(run, {kind: "error", status: "服务器未确认停止，正在断开当前会话的输出连接…"});
      }
    }
    catch { /* 网络失败仍由超时回退中止本地流 */ }
  };

  const copyMessage = async (message: Msg) => {
    const generation = historyGeneration.current;
    let ok = false;
    try {
      await navigator.clipboard.writeText(message.content);
      ok = true;
    } catch { /* Report the real clipboard failure in the selected conversation. */ }
    if (historyGeneration.current === generation) setCopyState({ message, ok });
  };
  useEffect(() => {
    if (!copyState) return;
    const timer = window.setTimeout(() => setCopyState(null), 1500);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const centerChatEntry = (id: string) => {
    const list = messageListRef.current;
    const entry = list?.querySelector(`#${id}`);
    if (!list || !entry) return;
    followsLatest.current = false;
    const rect = entry.getBoundingClientRect();
    const top = list.scrollTop + rect.top - list.getBoundingClientRect().top;
    list.scrollTo({ top: top - (list.clientHeight - rect.height) / 2, behavior: "smooth" });
  };

  const goToMessage = (index: number) => {
    const message = msgs[index];
    if (!message) return;
    setHighlightMessage(message);
    centerChatEntry(`chat-message-${index}`);
  };

  const goToTool = (index: number) => {
    const tool = toolCards[index];
    if (!tool) return;
    setHighlightTool(tool.id ?? tool);
    centerChatEntry(`chat-tool-${index}`);
  };

  const goToBottom = () => {
    followsLatest.current = true;
    const list = messageListRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  };

  const contextUsage = useMemo(
    () => estimateContextUsage(msgs, input, streamText, toolCards),
    [msgs, input, streamText, toolCards],
  );
  const currentConv = convs.find((conv) => conv.id === convId && conv.case_id === caseId);
  const environment = environmentPreferences?.cid === convId ? environmentPreferences : null;
  const savedPreference = environment?.preferences[backend];
  const profileStale = environment?.stale.includes(backend);
  useEffect(() => {
    if (!currentConv || loadingConv) return;
    setProfileId(savedPreference?.provider_profile_id ?? currentConv.provider_profile_id ?? null);
    setConvModel(savedPreference?.model ?? currentConv.model ?? "");
    setReasoningEffort(savedPreference?.reasoning_effort ?? "off");
    setNativeModelChoices({ ...defaultNativeChoices, ...savedPreference?.native_options });
  }, [convId, backend, savedPreference, loadingConv]);
  const savingSettings = convId !== null && savingSettingsId === convId;
  const settingsBusy = streaming || nativeBusy || loadingConv || savingSettings;
  const settingsChanged = Boolean(currentConv && (profileStale || profileId !== (savedPreference?.provider_profile_id ?? currentConv.provider_profile_id)
    || convModel.trim() !== (savedPreference?.model ?? currentConv.model) || permMode !== (currentConv.permission_mode ?? "approve_writes")
    || (backend === "api" && reasoningEffort !== (savedPreference?.reasoning_effort ?? "off"))
    || (backend !== "api" && Object.entries(nativeModelChoices).some(([key, value]) =>
      value !== (savedPreference?.native_options?.[key as keyof NativeModelChoices] ?? "")))));

  const changeCards = toolCards.map((card, index) => ({ card, index }))
    .filter(({ card }) => card.tool === "propose_file_update" || card.tool === "propose_text_replacement");

  if (!currentCase)
    return <div style={s.hint}>选择一个算例后使用 Agent</div>;
  if (!conversationScope || loadedConversationScope !== conversationScope)
    return <div style={s.hint} role={conversationLoadError === conversationScope ? "alert" : "status"}>
      {conversationLoadError === conversationScope ? "未能读取当前算例的会话，请重试切换算例。" : "正在读取当前算例的会话…"}
    </div>;

  return (
    <div className="agent-panel" style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 13, color: "var(--vf-text)" }}>
      <div className="agent-main-controls">
        <select aria-label="执行后端" data-testid="agent-backend" style={s.select} value={executionBackend}
          disabled={streaming || nativeBusy} onChange={e => {
            setBackend(e.target.value as Backend | AcpHarness);
            if (user && convId) localStorage.setItem(`vf-chat-backend:${user.id}:${convId}`, e.target.value);
          }}><option value="api">API Agent</option><option value="windows">Codex · Windows</option><option value="wsl">Codex · WSL</option>
          {(dockerAvailable || backend === "docker") && <option value="docker" disabled={!dockerAvailable}>Codex · Docker</option>}
          <option value="opencode">OpenCode</option><option value="kimi">Kimi Code</option></select>
        <select aria-label="当前会话" data-testid="agent-conversation" disabled={loadingConv} style={s.select} value={convId ?? ""}
          onChange={e => { const id = Number(e.target.value); convIdRef.current = id; setConvId(id); }}>
          {convs.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}
        </select>
        <button style={s.miniBtn} onClick={newConv} disabled={!profileId || nativeBusy || streaming}>新会话</button>
      </div>
      <details className="agent-settings" hidden={!!acpHarness}><summary>会话设置与历史操作</summary>
      {profileStale && <div role="status">保存的模型配置已变化，请核对当前后端的模型和推理设置。</div>}
      {/* profile 栏 */}
      <div className="agent-settings-controls" style={s.bar}>
        {backend === "api" && <ModelContextUsage key={convId} estimate={contextUsage} streaming={streaming}
          usage={convId && modelUsage?.key === runKey(conversationScope, convId) ? modelUsage.value : undefined} />}
        <select aria-label="模型配置" data-testid="agent-profile" disabled={settingsBusy}
                style={{ ...s.select, flex: "1 1 132px", minWidth: 112 }} value={profileId ?? ""}
                onChange={(e) => { const id = Number(e.target.value); setProfileId(id); setConvModel(profiles.find(p => p.id === id)?.default_model ?? ''); }}>
          <option value="" disabled>{profiles.length ? "选择模型配置" : "无模型配置"}</option>
          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.default_model}</option>)}
        </select>
        <button style={s.miniBtn} disabled={streaming || nativeBusy} onClick={() => setShowNewProfile(!showNewProfile)}>+</button>
        <button data-testid="profile-edit" style={s.ghostBtn} disabled={streaming || nativeBusy}
                onClick={() => setEditProfile(!editProfile)}>编辑</button>
        <button data-testid="profile-models" style={s.ghostBtn} onClick={loadModelOptions}>模型</button>
        <input aria-label="会话模型" data-testid="conv-model" list="provider-model-options" value={convModel}
               disabled={settingsBusy} maxLength={200} onChange={(e) => setConvModel(e.target.value)}
               title="会话模型。留空使用模型配置的默认值"
               style={{ ...s.select, flex: "0 1 110px", minWidth: 76 }} />
        <datalist id="provider-model-options">
          {modelOptions.map((model) => <option key={model.id} value={model.id} />)}
        </datalist>
        <input ref={importInputRef} data-testid="agent-import-file" type="file" accept=".jsonl,.ndjson"
               style={{ display: "none" }} onChange={(event) => importConv(event.target.files?.[0])} />
        <button data-testid="agent-import" style={s.ghostBtn} disabled={streaming || nativeBusy}
                onClick={() => importInputRef.current?.click()}
                title="导入当前/旧版 Agent JSONL；只恢复历史，不恢复活动任务或凭据">导入</button>
        <label style={s.replySound} title="在 Agent 完成一轮回复后播放声音">
          <input type="checkbox" checked={soundEnabled} onChange={(event) => {
            if (userId == null) return;
            const enabled = event.target.checked;
            setReplySoundEnabled(userId, enabled);
            setSoundEnabled(enabled);
          }} /> 回复提示音
        </label>
        <select aria-label="Agent 权限" data-testid="agent-permission-mode"
                style={{ ...s.select, flex: "0 0 auto", maxWidth: 112 }} value={permMode}
                disabled={settingsBusy}
                onChange={(e) => setPermMode(e.target.value)}
                title={backend === "api" ? "API 写入需逐次批准。问答不使用工具，计划可使用只读工具。"
                  : backend === "docker" ? "可写模式在容器内编辑算例副本，完成后核对原件版本并发布。"
                    : "Codex 工作区可写模式在超出沙箱时请求审批。问答和计划均使用只读沙箱。"}>
          <option value="approve_writes">{backend === "api" ? "逐次审批" : backend === "docker" ? "工作副本可写" : "工作区可写"}</option>
          <option value="full_access">{backend === "api" ? "全自动" : backend === "docker" ? "容器内自动" : "完全访问"}</option>
          <option value="ask">{backend === "api" ? "问答" : "只读问答"}</option>
          <option value="plan">只读计划</option>
        </select>
        <button type="button" data-testid="session-settings-save" style={s.ghostBtn} onClick={saveSessionSettings}
                disabled={!profileId || !settingsChanged || settingsBusy || savingSettingsId !== null
                  || pendingTools.length > 0 || compressingId === convId}>
          {savingSettings ? "保存中…" : "保存此会话"}
        </button>
        <select data-testid="reasoning-effort" style={{ ...s.select, flex: "0 0 auto", maxWidth: 104 }}
                value={reasoningEffort} disabled={backend !== "api" || streaming || nativeBusy} onChange={(e) => setReasoningEffort(e.target.value)}
                title="选择后可保存到当前后端；off 沿用默认推理参数">
          <option value="off">推理 off</option>
          <option value="low">推理 low</option>
          <option value="medium">推理 medium</option>
          <option value="high">推理 high</option>
        </select>
        {backend === "api" && <button style={s.ghostBtn} onClick={forkConv} disabled={!convId || streaming || nativeBusy}
                title="从当前会话分叉出一个分支（历史共享，互不影响）">分叉</button>}
        <button style={s.ghostBtn} onClick={compressConv} disabled={!convId || streaming || nativeBusy || !profileId || compressingId !== null}
                title="把早期对话压缩成 LLM 摘要，释放上下文">{compressingId !== null && compressingId === convId ? "压缩中…" : "压缩"}</button>
        {backend === "api" && <button data-testid="handoff" style={s.ghostBtn} onClick={handoffConv} disabled={!convId || streaming || nativeBusy || compressingId === convId}
                title="以最近压缩摘要或有界文本开启新会话，不复制完整历史">交接</button>}
        <button data-testid="agent-rename" style={s.ghostBtn} onClick={renameConv}
                disabled={!convId || streaming || nativeBusy}>重命名</button>
        <button data-testid="agent-export" style={s.ghostBtn} onClick={exportConv}
                disabled={!convId || streaming || nativeBusy || pendingTools.length > 0}
                title="导出完整可见历史为版本化 JSONL；运行中或待审批时禁用">导出</button>
        <button data-testid="agent-delete" style={s.ghostBtn} onClick={deleteConv}
                disabled={!convId || streaming || nativeBusy || pendingTools.length > 0}
                title="运行中或待审批时禁止删除；只删除会话记录，不删除算例文件">删除</button>
      </div>

      </details>

      {currentConv?.permission_mode === "plan" && <div style={s.planApproval}>
        <span>只读计划</span>
        <button data-testid="approve-plan" style={s.ghostBtn} onClick={approvePlan}
                disabled={streaming || nativeBusy || pendingTools.length > 0}
                title={backend === "api" ? "退出只读计划。后续写入仍需逐次审批。"
                  : backend === "docker" ? "退出只读计划并允许编辑容器中的算例副本。"
                    : "退出只读计划并允许工作区写入。超出沙箱的操作需审批。"}>批准 Plan</button>
      </div>}
      {profileStatus && <div data-testid="profile-status" role="status" style={s.profileStatus}>{profileStatus}</div>}
      {editProfile && !streaming && !nativeBusy && (
        <div style={s.profileForm}>
          <select value={ep.provider} onChange={(e) => setEp({ ...ep, provider: e.target.value })}>
            {Object.keys(providers).map((key) => <option key={key} value={key}>{key}</option>)}
          </select>
          <input data-testid="profile-edit-name" placeholder="名称" value={ep.name}
                 onChange={(e) => setEp({ ...ep, name: e.target.value })} />
          <input data-testid="profile-base-url" placeholder="base_url" value={ep.base_url}
                 onChange={(e) => setEp({ ...ep, base_url: e.target.value })} />
          <input data-testid="profile-default-model" placeholder="默认模型" value={ep.default_model}
                 onChange={(e) => setEp({ ...ep, default_model: e.target.value })} />
          <input placeholder="API Key（留空保持不变）" type="password" value={ep.api_key}
                 onChange={(e) => setEp({ ...ep, api_key: e.target.value })} />
          <div style={{ display: "flex", gap: 6 }}>
            <button data-testid="profile-save" style={s.btn} onClick={saveEditedProfile}>保存</button>
            <button data-testid="profile-test" style={s.miniBtn} onClick={testProfile}>测试连接</button>
            <button style={s.ghostBtn} onClick={() => setEditProfile(false)}>取消</button>
          </div>
        </div>
      )}
      {showNewProfile && !streaming && !nativeBusy && (
        <div style={s.profileForm}>
          <select value={np.provider} onChange={(e) =>
            setNp({ ...np, provider: e.target.value, base_url: providers[e.target.value]?.base_url ?? "" })}>
            {Object.keys(providers).map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <input placeholder="名称" value={np.name} onChange={(e) => setNp({ ...np, name: e.target.value })} />
          <input placeholder="base_url（custom 必填）" value={np.base_url}
                 onChange={(e) => setNp({ ...np, base_url: e.target.value })} />
          <input placeholder="默认模型" value={np.default_model}
                 onChange={(e) => setNp({ ...np, default_model: e.target.value })} />
          <input placeholder="API Key（只写库不回显）" type="password" value={np.api_key}
                 onChange={(e) => setNp({ ...np, api_key: e.target.value })} />
          <button style={s.btn} onClick={createProfile}>保存 profile</button>
        </div>
      )}

      {/* 消息流 */}
      {acpHarness && currentConv && convId && user ? <AcpChatPanel key={`${user.id}:${convId}:${acpHarness}`}
        conversationId={convId} caseId={currentCase.id} harness={acpHarness} profiles={profiles} initialProfileId={profileId} onBusy={setNativeBusy}
        /> : backend !== "api" && currentConv && convId && user ? <NativeChatPanel key={`${user.id}:${convId}:${backend}`}
        conversationId={convId} caseId={currentCase.id} userId={user.id} runtime={backend}
        profileId={profileId} model={convModel} permission={permMode} onBusy={setNativeBusy}
        modelChoices={nativeModelChoices} onModelChoices={setNativeModelChoices}
        onBranch={async (id, runtime) => {
          const branch = await api.get<Conv>(`/conversations/${id}`);
          localStorage.setItem(`vf-chat-backend:${user.id}:${id}`, runtime);
          setConvs(items => [branch, ...items.filter(item => item.id !== id)]);
          convIdRef.current = id; setConvId(id); setBackend(runtime === "docker" || runtime === "wsl" ? runtime : "windows");
        }} /> : <>
      <div ref={messageListRef} data-testid="chat-message-list" style={{ flex: 1, overflow: "auto", padding: 10 }}
           onScroll={({ currentTarget: list }) => {
             followsLatest.current = list.scrollHeight - list.clientHeight - list.scrollTop <= 24;
           }}>
        <div style={s.chatNav}>
          <div style={s.chatNavControls}>
            {olderPage !== null && (
              <button data-testid="chat-load-older" style={s.ghostBtn} disabled={olderLoading} onClick={loadOlder}>
                {olderLoading ? "加载中…" : "加载更早"}
              </button>
            )}
            <select data-testid="chat-user-nav" aria-label="跳到指定用户消息" style={{ minWidth: 0, flex: "1 1 100px" }}
                    value="" onChange={(event) => {
              if (event.target.value) goToMessage(Number(event.target.value));
            }} title="跳到指定用户消息">
              <option value="">跳到用户消息…</option>
              {msgs.map((m, i) => m.role === "user" ? (
                <option key={i} value={i}>用户 {i + 1}：{m.content.slice(0, 30)}</option>
              ) : null)}
            </select>
            <button data-testid="chat-changes" style={s.ghostBtn} disabled={!changeCards.length}
                    aria-expanded={changesOpen && changeCards.length > 0}
                    onClick={() => setChangesOpen(!changesOpen)}>修改摘要 {changeCards.length}</button>
            <button data-testid="chat-bottom" style={s.ghostBtn} onClick={goToBottom}>回底部</button>
          </div>
          {changesOpen && changeCards.length > 0 && (
            <div data-testid="chat-change-list" style={s.changeList}>
              {changeCards.map(({ card, index }) => (
                <button key={card.id ?? index} data-testid="chat-change-item" data-tool-index={index}
                        onClick={() => goToTool(index)} style={s.changeItem}>
                  {card.tool} · {card.result.slice(0, 100)}
                </button>
              ))}
            </div>
          )}
        </div>
        {msgs.map((m, i) => (
          m.role === "summary" ? (
            <div key={m.id == null ? `local-${i}` : `event-${m.id}`} style={s.summary}>
              📦 上下文摘要（早前历史已压缩）<div style={{ marginTop: 4 }}>{m.content}</div>
            </div>
          ) : (
            <div key={m.id == null ? `local-${i}` : `event-${m.id}`} id={`chat-message-${i}`} data-testid="chat-message" data-index={i}
                 data-highlight={highlightMessage === m}
                 style={{ ...s.msg, ...(m.role === "user" ? s.user : s.assistant),
                          ...(highlightMessage === m ? s.highlight : {}) }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                <div style={s.role}>
                  <span>{m.role === "user" ? "我" : `Agent${m.cancelled ? " · 已取消" : m.failure === "timed_out" ? " · 已超时" : m.failure === "failed" ? " · 失败" : m.interrupted ? " · 已中断" : ""}`}</span>
                  {m.role === "assistant" && m.run_id && runTimings[m.run_id] &&
                    <RunDurationLabel timing={runTimings[m.run_id]} />}
                </div>
                <span style={{ display: "inline-flex", gap: 3 }}>
                  {m.role === "assistant" && m.canFork !== false && !m.cancelled && !m.interrupted && !m.tool_calls?.length && (
                    <button data-testid="chat-branch" data-index={i} style={s.copyBtn}
                            disabled={streaming} title="从此已完成回答创建分支"
                            onClick={() => forkFromMessage(i)}>分支</button>
                  )}
                  <button data-testid="chat-copy" data-index={i} style={s.copyBtn}
                          onClick={() => copyMessage(m)}>
                    {copyState?.message === m ? (copyState.ok ? "已复制" : "失败") : "复制"}
                  </button>
                </span>
              </div>
              {m.reasoning && <ReasoningCard text={m.reasoning} streaming={false} />}
              {m.images && m.images.length > 0 && <MessageImages images={m.images} caseId={currentCase.id} />}
              {m.role === "user" ? (
                <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
              ) : m.cancelled && !m.content ? (
                <div style={{ color: "#8b93a7", fontSize: 12 }}>（本回合已取消，无可见正文）</div>
              ) : (
                <ChatMarkdown text={m.content} />
              )}
            </div>
          )
        ))}

        {/* 工具结果卡片 */}
        {toolCards.map((c, i) => {
          const status = c.status ?? (c.rejected ? "cancelled" : c.ok ? "completed" : "error");
          const presentation = toolPresentation(c.tool, status);
          const proposal = (c.tool === "propose_file_update" || c.tool === "propose_text_replacement") &&
            (status === "completed" || status === "error" || status === "cancelled" || status === "interrupted");
          return (
          <div key={`tc-${c.id ?? i}`} id={`chat-tool-${i}`} data-testid="chat-tool-card" data-index={i}
               data-status={status} data-kind={presentation.kind}
               data-highlight={highlightTool === (c.id ?? c)} style={{ ...s.toolCard, ...(highlightTool === (c.id ?? c) ? s.highlight : {}) }}>
            <div style={{ fontSize: 11, color: "#667" }}>
              {TOOL_KIND_ICONS[presentation.kind]} {presentation.label} · {toolStatusLabel(status)}
              {proposal && (c.auto ? " · 自动应用" : c.approved ? " · 人工批准" : c.rejected ? " · 已拒绝" : "")}
            </div>
            {c.result && <pre style={s.toolPre}>{c.result.slice(0, 800)}</pre>}
            {proposal && <ProposalHistory card={c} />}
          </div>
          );
        })}

        {/* 待审批卡片（带 diff 预览） */}
        {pendingTools.map((tc) => (
          <ApprovalCard key={tc.id} tc={tc} busy={loadingConv || streaming || (convId !== null && approvalBusy.has(convId))} onDecide={decide} />
        ))}

        {streamReasoning && <ReasoningCard text={streamReasoning} streaming />}
        {streamText && (
          <div style={{ ...s.msg, ...s.assistant }}>
            <div style={s.role}>Agent{activeRunRef.current && runTimings[activeRunRef.current] &&
              <RunDurationLabel timing={runTimings[activeRunRef.current]} />}</div>
            <ChatMarkdown text={streamText} streaming />
          </div>
        )}
      </div>

      <GoalStrip goal={goal} onRun={runGoal} busy={streaming || loadingConv} />

      <PlanStrip steps={plan} />

      {/* 阶段快捷提示词 */}
      <div style={s.stageRow}>
        {STAGE_PROMPTS.map(([label, prompt]) => (
          <button key={label} style={s.stageBtn} title={prompt}
                  onClick={() => updateInput(prompt)} disabled={!convId}>
            {label}
          </button>
        ))}
      </div>

      {/* 输入（$ 触发技能补全） */}
      <DraftImages images={attachments} onRemove={index => useStore.getState().updateAgentDraft(current => ({
        ...current, images: current.images.filter((_, i) => i !== index),
      }))} />
      <div style={s.inputRow}>
        <div style={{ flex: "1 0 100%", minWidth: 0, position: "relative" }}>
          <textarea
            data-testid="agent-input"
            style={{ ...s.textarea, width: "100%" }}
            placeholder={convId ? "描述你的仿真需求…（$ 可调技能；试试：列出文件 / 帮我跑一下）" : "先点「新会话」"}
            value={input}
            disabled={!convId || streaming || loadingConv}
            onChange={(e) => {
              const v = e.target.value;
              updateInput(v);
              const m = v.match(/(?:^|\s)\$([\w-]*)$/);  // 光标前的 $前缀
              setSkillQuery(m ? m[1] : null);
              const f = v.match(/(?:^|\s)@([\w\-./]*)$/);  // 光标前的 @路径前缀
              setFileQuery(f ? f[1] : null);
            }}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
            onPaste={(e) => {
              const files = Array.from(e.clipboardData.items)
                .filter((item) => item.kind === "file")
                .map((item) => item.getAsFile())
                .filter((file): file is File => Boolean(file));
              if (files.length) {
                e.preventDefault();
                void addImages(files);
              }
            }}
          />
          {skillQuery !== null && skills.length > 0 && (
            <div style={s.skillPop}>
              {matchingSkills.map((sk) => (
                <div key={sk.name} style={s.skillItem} title={sk.default_prompt ?? sk.description}
                     onClick={() => {
                       if (sk.default_prompt) {
                         updateInput(sk.default_prompt + " ");
                       } else {
                         updateInput(input.replace(/(?:^|\s)\$([\w-]*)$/, (m) =>
                           m.replace(/\$[\w-]*$/, `$${sk.name} `)));
                       }
                       setSkillQuery(null);
                     }}>
                  <b>{sk.display_name ?? `$${sk.name}`}</b> <span style={{ color: "#667" }}>{(sk.short_description ?? sk.description).slice(0, 40)}</span>
                </div>
              ))}
              {matchingSkills.length === 0 && (
                <div style={{ ...s.skillItem, color: "#667" }}>无匹配技能</div>
              )}
            </div>
          )}
          {fileQuery !== null && (
            <div style={s.skillPop}>
              {useStore.getState().fileTree
                .filter((t) => t.path.toLowerCase().includes(fileQuery.toLowerCase()))
                .slice(0, 8)
                .map((t) => (
                  <div key={t.path} style={s.skillItem}
                       onClick={() => {
                         updateInput(input.replace(/(?:^|\s)@([\w\-./]*)$/, (m) =>
                           m.replace(/@[\w\-./]*$/, `@${t.path} `)));
                         setFileQuery(null);
                       }}>
                    📄 {t.path}
                  </div>
                ))}
              {fileQuery !== null && !useStore.getState().fileTree.some(
                (t) => t.path.toLowerCase().includes(fileQuery.toLowerCase())) && (
                <div style={{ ...s.skillItem, color: "#667" }}>无匹配文件（@ 引用算例内文件）</div>
              )}
            </div>
          )}
        </div>
        <input ref={fileInputRef} data-testid="agent-image-input" type="file" multiple
               accept=".png,.jpg,.jpeg,.gif,.webp" style={{ display: "none" }}
               onChange={(event) => {
                 void addImages(Array.from(event.target.files ?? []));
                 event.target.value = "";
               }} />
        <button data-testid="agent-voice" style={s.ghostBtn}
                aria-pressed={voiceStatus === "listening"}
                title={voiceCapabilities?.server_transcription ? "录音将发送到平台服务进行转写" : "使用浏览器语音识别"}
                disabled={!convId || streaming || loadingConv} onClick={toggleVoice}>
          {voiceStatus === "listening" ? "停止录音" : voiceStatus === "processing" ? "取消转写" :
            voiceStatus === "starting" ? "取消开启麦克风" : "语音输入"}
        </button>
        {voiceStatus.startsWith("error:") && <span role="alert" data-testid="agent-voice-error">
          {voiceError(voiceStatus.slice(6))}
        </span>}
        <button data-testid="agent-image-button" style={s.ghostBtn}
                disabled={!convId || streaming || loadingConv || attachments.length >= maxMessageImages}
                onClick={() => fileInputRef.current?.click()}>图片</button>
        <button data-testid="agent-send" style={s.btn} onClick={send}
                disabled={!convId || streaming || loadingConv || compressingId === convId || savingSettings
                  || voiceStatus === "starting" || voiceStatus === "listening" || voiceStatus === "processing"
                  || (!input.trim() && !attachments.length)}>
          {streaming ? "…" : "发送"}
        </button>
        <button data-testid="agent-stop" style={s.rejectBtn} disabled={!streaming} onClick={stopRun}>停止</button>
      </div>
      </>}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  hint: { padding: 16, color: "#8b93a7" },
  bar: { display: "flex", gap: 6, padding: 8, borderBottom: "1px solid #2a2f3a", alignItems: "center" },
  select: { flex: 1, minWidth: 0, padding: "4px 6px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 6, fontSize: 12 },
  miniBtn: { padding: "4px 10px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, whiteSpace: "nowrap" },
  profileForm: { display: "flex", flexDirection: "column", gap: 6, padding: 8, borderBottom: "1px solid #2a2f3a", background: "#14171d" },
  profileStatus: { padding: "4px 8px", color: "#8fa8cc", fontSize: 11, borderBottom: "1px solid #1a1e27" },
  planApproval: { display: "flex", flexWrap: "wrap", justifyContent: "space-between", alignItems: "center",
                  gap: 6, padding: "6px 8px", borderBottom: "1px solid var(--vf-border)", fontSize: 12 },
  msg: { margin: "6px 0", padding: "8px 10px", borderRadius: 8, maxWidth: "92%" },
  summary: { margin: "6px 0", padding: "6px 10px", borderRadius: 8, background: "#141a24",
             border: "1px dashed #3b82f655", color: "#8fa8cc", fontSize: 12,
             whiteSpace: "pre-wrap" },
  ghostBtn: { padding: "4px 10px", background: "#1a1e27", color: "#9ab",
              border: "1px solid #2a2f3a", borderRadius: 6, cursor: "pointer",
              fontSize: 12, whiteSpace: "nowrap" },
  user: { background: "#1d3557", marginLeft: "auto" },
  assistant: { background: "#1a1e27" },
  role: { fontSize: 11, color: "#667", marginBottom: 4 },
  chatNav: { position: "sticky", top: -10, zIndex: 5, display: "grid", gridTemplateColumns: "minmax(0, 1fr)",
             gap: 5, padding: "4px 0 7px",
             background: "#0f1115", borderBottom: "1px solid #1a1e27" },
  chatNavControls: { display: "flex", flexWrap: "wrap", gap: 5, minWidth: 0 },
  replySound: { display: "inline-flex", alignItems: "center", gap: 3, whiteSpace: "nowrap", fontSize: 12 },
  changeList: { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 3, padding: 5,
                border: "1px solid #22304a", borderRadius: 6,
                background: "#10141c", maxHeight: "min(26vh, 160px)", overflowY: "auto" },
  changeItem: { padding: "4px 6px", background: "transparent", color: "#9ab3d0", border: 0,
                textAlign: "left", cursor: "pointer", fontSize: 11, overflowWrap: "anywhere" },
  highlight: { outline: "1px solid #3b82f6", boxShadow: "0 0 0 2px #3b82f622" },
  copyBtn: { padding: "1px 5px", background: "transparent", color: "#667", border: 0,
             cursor: "pointer", fontSize: 10 },
  reasoning: { margin: "4px 0 6px", border: "1px solid #3b82f633", borderRadius: 6, background: "#111823" },
  reasoningToggle: { width: "100%", padding: "4px 7px", background: "transparent", color: "#8fa8cc",
                     border: 0, textAlign: "left", cursor: "pointer", fontSize: 11 },
  reasoningContent: { padding: "0 8px 7px", color: "#9ab3d0", fontSize: 11, whiteSpace: "pre-wrap",
                      maxHeight: 180, overflow: "auto" },
  toolCard: { margin: "6px 0", padding: "6px 10px", borderRadius: 8, background: "#10141c", border: "1px solid #22304a" },
  toolPre: { margin: "4px 0 0", fontSize: 11, color: "#9ab3d0", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 160, overflow: "auto" },
  approval: { margin: "6px 0", padding: "8px 10px", borderRadius: 8, background: "#231d12", border: "1px solid #fbbf2455" },
  approveBtn: { padding: "4px 12px", background: "#16a34a", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  disabledApprovalBtn: { background: "var(--vf-panel, #303036)", color: "var(--vf-muted, #8b93a7)", cursor: "not-allowed" },
  rejectBtn: { padding: "4px 12px", background: "#7f1d1d", color: "#fca5a5", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  planStrip: { display: "flex", gap: "4px 12px", flexWrap: "wrap", padding: "6px 10px", margin: 0,
               borderTop: "1px solid var(--vf-border)", background: "var(--vf-panel)", listStyle: "none",
               color: "var(--vf-text)", fontSize: 13, maxHeight: 112, overflow: "auto" },
  planStep: { display: "flex", alignItems: "baseline", gap: 6, minWidth: 0, overflowWrap: "anywhere" },
  goalStrip: { display: "grid", gap: 4, padding: "6px 10px", borderTop: "1px solid var(--vf-border)",
               background: "var(--vf-panel)", color: "var(--vf-text)", fontSize: 13, overflowWrap: "anywhere",
               maxHeight: 144, overflow: "auto" },
  goalRunBtn: { justifySelf: "start", padding: "3px 10px", background: "#2563eb", color: "#fff",
                border: 0, borderRadius: 5, cursor: "pointer", fontSize: 11 },
  stageRow: { display: "flex", gap: 4, padding: "4px 8px", borderTop: "1px solid #1a1e27",
              flexWrap: "wrap" },
  stageBtn: { padding: "2px 8px", background: "#141a24", color: "#8fa8cc",
              border: "1px solid #22304a", borderRadius: 5, cursor: "pointer", fontSize: 11 },
  skillPop: { position: "absolute", bottom: "100%", left: 0, right: 0, maxHeight: 200,
              overflow: "auto", background: "#14171d", border: "1px solid #2a2f3a",
              borderRadius: 8, marginBottom: 4, boxShadow: "0 -6px 20px #000a", zIndex: 20 },
  skillItem: { padding: "6px 10px", fontSize: 12, cursor: "pointer",
               borderBottom: "1px solid #1a1e27" },
  inputRow: { display: "flex", flexWrap: "wrap", gap: 6, padding: 8, borderTop: "1px solid #2a2f3a" },
  textarea: { flex: 1, minHeight: 44, maxHeight: 120, padding: 8, background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 8, resize: "vertical", fontSize: 13 },
  btn: { padding: "0 16px", background: "#3b82f6", color: "#fff", border: "none", borderRadius: 8, cursor: "pointer" },
};
