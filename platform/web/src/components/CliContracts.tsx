import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { activatePanel } from "../shell/dock";
import { providerProfilesChanged } from "../panels/ProviderProfilesPanel";
import "./CliContracts.css";

export const cliContractsChanged = "vibeflow:cli-contracts-changed";

type Harness = "claude" | "codex" | "opencode" | "kimi";
interface Profile { id: number; name: string; default_model: string; has_key: boolean; shared?: boolean }
interface ContractState {
  revision: string; selected: Harness | null; instructions: string; target: string | null; applied: boolean;
  provider_profile_id: number | null;
  backups: { id: string; created_at: string; selected: Harness | null; provider_profile_id: number | null }[];
  backup_limit: number;
}

export default function CliContracts() {
  const caseId = useStore(s => s.currentCase?.id);
  const userId = useStore(s => s.user?.id);
  const scope = `${userId}:${caseId}`;
  const active = useRef(scope); active.current = scope;
  const [state, setState] = useState<ContractState | null>(null);
  const [harness, setHarness] = useState<Harness>("opencode");
  const [instructions, setInstructions] = useState("");
  const [profileId, setProfileId] = useState<number | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profileError, setProfileError] = useState(false);
  const [backup, setBackup] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const sequence = useRef(0);
  const path = `/cases/${caseId}/cli-contract`;
  async function load() {
    const serial = ++sequence.current;
    try {
      const value = await api.get<ContractState>(path);
      if (active.current !== scope || sequence.current !== serial) return;
      setState(value); setHarness(value.selected ?? "opencode"); setInstructions(value.instructions);
      setProfileId(value.provider_profile_id);
      setBackup(value.backups[0]?.id ?? ""); setError("");
    } catch (failure) {
      if (active.current === scope && sequence.current === serial) setError(failure instanceof Error ? failure.message : "读取失败，请重试。");
    }
  }
  useEffect(() => {
    setState(null); setError(""); setMessage(""); setInstructions(""); setBusy(false);
    if (caseId) void load();
    return () => { sequence.current++; };
  }, [caseId, userId]);
  useEffect(() => {
    if (!userId || !caseId) return;
    const controller = new AbortController();
    setProfiles([]);
    const read = () => api.get<Profile[]>('/provider-profiles', undefined, controller.signal).then(items => {
      setProfiles(items.filter(item => !item.shared && item.has_key && item.default_model));
      setProfileError(false);
    }).catch(() => { if (!controller.signal.aborted) setProfileError(true); });
    void read();
    window.addEventListener(providerProfilesChanged, read);
    return () => { controller.abort(); window.removeEventListener(providerProfilesChanged, read); };
  }, [caseId, userId]);
  async function submit(restore: boolean) {
    if (!state || busy || (restore && !backup)) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await api.post(restore ? `${path}/backups/${backup}:restore` : path,
        { expected_revision: state.revision, ...(restore ? {} : {
          harness, instructions, provider_profile_id: harness === 'claude' ? null : profileId,
        }) });
      window.dispatchEvent(new Event(cliContractsChanged));
      if (active.current !== scope) return;
      await load();
      if (active.current === scope) setMessage(restore ? "已恢复所选备份；恢复前的内容也已备份。" : "契约已保存，原内容已备份。请新建 CLI 会话读取新约定。");
    } catch (failure) {
      if (active.current === scope) setError(failure instanceof Error ? failure.message : "操作失败，请重新读取配置。");
    } finally { if (active.current === scope) setBusy(false); }
  }
  return <section className="cli-contracts" aria-label="CLI 契约与备份" data-testid="cli-contracts">
    <h3>CLI 契约与备份</h3>
    {!caseId ? <p>先选择算例，再配置该算例的 CLI 工作约定。</p> : <>
      <p>保存当前算例的工作约定和默认模型配置引用。备份保留原有说明文件。</p>
      {error && <p role="alert">{error}</p>}
      <button type="button" disabled={busy} onClick={() => void load()}>重新读取契约</button>
      {state ? <>
        <p>当前：{state.selected ?? "未选择"} · {state.applied ? `已写入 ${state.target}` : "尚未应用契约"}</p>
        <label>CLI 工具<select aria-label="CLI 工具" value={harness} disabled={busy} onChange={e => {
          const selected = e.target.value as Harness;
          setHarness(selected);
          setProfileId(selected === state.selected ? state.provider_profile_id : null);
        }}>
          <option value="claude">Claude</option><option value="codex">Codex</option>
          <option value="opencode">OpenCode</option><option value="kimi">Kimi</option>
        </select></label>
        {harness !== 'claude' && <label>默认模型配置
          <select aria-label="CLI 默认模型配置" value={profileId ?? ''} disabled={busy || profileError}
            onChange={event => setProfileId(event.target.value ? Number(event.target.value) : null)}>
            <option value="">启动时选择本人配置</option>
            {profileId !== null && !profiles.some(item => item.id === profileId) &&
              <option value={profileId}>已保存的配置暂不可用</option>}
            {profiles.map(item => <option value={item.id} key={item.id}>{item.name} · {item.default_model}</option>)}
          </select>
        </label>}
        {profileError && harness !== 'claude' && <p role="status">模型配置暂不可读，请检查模型提供商设置。</p>}
        <label>补充工作约定<textarea aria-label="补充工作约定" value={instructions} maxLength={8000} rows={5} disabled={busy}
          onChange={e => setInstructions(e.target.value)} aria-describedby="cli-contract-help" /></label>
        <p id="cli-contract-help">平台约定会一并加入。这里填写算例规则，不要填写 API 密钥。运行中的会话不会自动切换。</p>
        <div className="cli-contract-actions">
          <button type="button" disabled={busy} onClick={() => void submit(false)}>{busy ? "处理中…" : "保存契约并备份"}</button>
          <button type="button" disabled={busy} onClick={() => activatePanel("terminal")}>前往终端</button>
        </div>
        <label>恢复备份<select aria-label="恢复备份" value={backup} disabled={busy || !state.backups.length} onChange={e => setBackup(e.target.value)}>
          {!state.backups.length && <option value="">尚无备份</option>}
          {state.backups.map(b => <option key={b.id} value={b.id}>{new Date(b.created_at).toLocaleString()} · {b.selected ?? "未选择"} · {b.id.slice(0, 6)}</option>)}
        </select></label>
        <button type="button" disabled={busy || !backup} onClick={() => void submit(true)}>恢复所选备份</button>
        <p>保留最近 {state.backup_limit} 次说明文件及配置引用。恢复前会先备份当前内容。</p>
      </> : !error && <p role="status">正在读取契约…</p>}
      {message && <p role="status">{message}</p>}
    </>}
  </section>;
}
