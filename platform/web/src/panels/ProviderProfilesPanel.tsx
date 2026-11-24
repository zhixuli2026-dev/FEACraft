import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useStore } from "../store";
import "./ProviderProfilesPanel.css";

export const providerProfilesChanged = "vibeflow:provider-profiles-changed";

type Profile = {
  id: number; provider: string; name: string; base_url: string; default_model: string;
  has_key: boolean; shared?: boolean; org_id?: number | null; can_manage?: boolean;
};
type Membership = { id: number; name: string; role: string };
type Model = { id: string };
type Draft = { provider: string; name: string; base_url: string; default_model: string; api_key: string };
type Registry = Record<string, { base_url: string }>;
const blank = (): Draft => ({ provider: "deepseek", name: "", base_url: "", default_model: "", api_key: "" });

export default function ProviderProfilesPanel() {
  const user = useStore((state) => state.user?.id);
  return user ? <ProviderWorkspace key={user} /> : null;
}

function ProviderWorkspace() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [registry, setRegistry] = useState<Registry>({});
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [organizationKms, setOrganizationKms] = useState(false);
  const [organization, setOrganization] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>(blank);
  const [models, setModels] = useState<Model[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [remove, setRemove] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const profile = profiles.find((item) => item.id === selected);
  const canManage = !profile?.shared || profile.can_manage === true;
  const adminScopes = memberships.filter((item) => item.role === "admin");
  const ownerName = memberships.find((item) => item.id === profile?.org_id)?.name;

  const choose = (item?: Profile) => {
    setSelected(item?.id ?? null);
    setOrganization(item?.org_id ?? null);
    setDraft(item ? { provider: item.provider, name: item.name, base_url: item.base_url,
      default_model: item.default_model, api_key: "" } : blank());
    setModels([]); setRemove(false); setStatus(""); setError("");
  };

  const load = async () => {
    setLoading(true); setError("");
    try {
      const [catalog, items, scopes, ownership] = await Promise.all([
        api.get<Registry>("/llm/providers"), api.get<Profile[]>("/provider-profiles"),
        api.get<Membership[]>("/org/memberships").catch(() => []),
        api.get<{ organization: boolean }>("/llm/profile-ownership").catch(() => ({organization:false})),
      ]);
      setRegistry(catalog);
      setProfiles(items);
      setMemberships(scopes);
      setOrganizationKms(ownership.organization);
      choose(items.find((item) => item.id === selected) ?? items[0]);
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const operation = async (task: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(""); setStatus("");
    try { await task(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  };

  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!canManage) return;
    void operation(async () => {
      const payload = { provider: draft.provider, name: draft.name.trim(), base_url: draft.base_url.trim(),
        default_model: draft.default_model.trim(), ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}) };
      const createPath = organization == null ? "/provider-profiles" : `/org/${organization}/provider-profiles`;
      const saved = selected == null ? await api.post<Profile>(createPath, payload)
        : await api.patch<Profile>(`/provider-profiles/${selected}`, payload);
      setProfiles((items) => [...items.filter((item) => item.id !== saved.id), saved]);
      choose(saved);
      setStatus("模型配置已保存");
      window.dispatchEvent(new Event(providerProfilesChanged));
    });
  };

  const check = (test: boolean) => void operation(async () => {
    if (selected == null) return;
    if (test) {
      const result = await api.post<{ latency_ms: number; model_count: number; models: Model[] }>(`/provider-profiles/${selected}:test`, {});
      setModels(result.models);
      setStatus(`连接通过 · ${result.model_count} 个模型 · ${result.latency_ms} ms`);
    } else {
      const result = await api.get<{ models: Model[]; fallback: boolean; error?: string }>(`/llm/models?provider_profile_id=${selected}`);
      setModels(result.models);
      if (result.fallback) setError(result.error || "模型列表暂时不可用，可输入模型名称");
      else setStatus(`已读取 ${result.models.length} 个模型`);
    }
  });

  const destroy = () => void operation(async () => {
    if (!profile || !canManage || !remove) return;
    await api.del(`/provider-profiles/${profile.id}`);
    const left = profiles.filter((item) => item.id !== profile.id);
    setProfiles(left); choose(left[0]); setStatus("模型配置已删除");
    window.dispatchEvent(new Event(providerProfilesChanged));
  });

  const rewrap = () => void operation(async () => {
    if (!profile?.org_id || !profile.can_manage) return;
    const result = await api.post<{ rewrapped: boolean }>(`/provider-profiles/${profile.id}:rewrap`, {});
    setStatus(result.rewrapped ? "已更新密钥加密版本" : "已使用当前加密版本");
  });

  return <section className="vf-provider-workspace" aria-label="模型配置" data-testid="provider-workspace">
    <header><h2>模型配置</h2><button type="button" disabled={loading || busy} onClick={() => choose()}>新建配置</button></header>
    {loading ? <p role="status">正在读取模型配置…</p> : <>
      <label>选择配置<select value={selected ?? ""} disabled={busy} onChange={(event) =>
        choose(profiles.find((item) => item.id === Number(event.target.value)))}>
        <option value="">新配置</option>
        {profiles.map((item) => <option key={item.id} value={item.id}>{item.name}{item.org_id ? " · 组织持有" :
          item.shared ? " · 组织共享" : ""}</option>)}
      </select></label>
      <form onSubmit={save}>
        <fieldset disabled={busy || !canManage}>
          {!profile && organizationKms && adminScopes.length > 0 && <label>配置归属
            <select value={organization ?? ""} onChange={(event) => setOrganization(Number(event.target.value) || null)}>
              <option value="">个人</option>
              {adminScopes.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>}
          {profile?.org_id && <p className="vf-provider-owner">
            组织持有 · {ownerName ?? `组织 ${profile.org_id}`} · KMS 加密
          </p>}
          <label>配置名称<input value={draft.name} required maxLength={120} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>服务商<select value={draft.provider} onChange={(event) => setDraft({ ...draft, provider: event.target.value, base_url: "" })}>
            {Object.keys(registry).map((provider) => <option key={provider}>{provider}</option>)}
          </select></label>
          <label>API 地址<input value={draft.base_url} placeholder={registry[draft.provider]?.base_url}
            required={draft.provider === "custom"} autoComplete="off" onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} /></label>
          <label>默认模型<input value={draft.default_model} maxLength={200} list="vf-provider-model-options"
            onChange={(event) => setDraft({ ...draft, default_model: event.target.value })} /></label>
          <datalist id="vf-provider-model-options">{models.map((model) => <option key={model.id} value={model.id} />)}</datalist>
          {canManage && <label>API 密钥<input type="password" value={draft.api_key} required={!profile}
            placeholder={profile?.has_key ? "留空保留已保存密钥" : "输入 API 密钥"} autoComplete="new-password"
            onChange={(event) => setDraft({ ...draft, api_key: event.target.value })} /></label>}
        </fieldset>
        <div className="vf-provider-actions">
          {canManage && <button className="vf-provider-save" disabled={busy || !draft.name.trim() || !Object.keys(registry).length}>保存配置</button>}
          {profile && <>
            <button type="button" disabled={busy} onClick={() => check(true)}>测试连接</button>
            <button type="button" disabled={busy} onClick={() => check(false)}>读取模型</button>
          </>}
        </div>
      </form>
      {!!models.length && <details><summary>可用模型</summary><ul>{models.map((model) => <li key={model.id}>{model.id}</li>)}</ul></details>}
      {!canManage && <p>{profile?.org_id ? "组织管理员管理此配置。" : "由组织共享，编辑由配置所有者处理。"}</p>}
      {profile && canManage && <div className="vf-provider-actions">
        {!!profile.org_id && <button type="button" disabled={busy || remove} onClick={rewrap}>更新密钥加密版本</button>}
        {remove ? <>
          <button type="button" disabled={busy} onClick={destroy}>确认删除配置</button>
          <button type="button" disabled={busy} onClick={() => setRemove(false)}>取消删除</button>
        </> : <button type="button" disabled={busy} onClick={() => setRemove(true)}>删除配置</button>}
      </div>}
    </>}
    {status && <p role="status">{status}</p>}
    {error && <div role="alert">{error}{!Object.keys(registry).length &&
      <button type="button" disabled={loading || busy} onClick={load}>重新读取配置</button>}</div>}
  </section>;
}
