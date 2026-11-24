import { useRef, useState } from "react";
import { api } from "../api";
import { useCaseDraft } from "../utils/useCaseDraft";
import type { CaseDictSnapshot, DictReceipt } from "../utils/useCaseDraft";
import { DraftReview, draftError } from "./CaseDraftFeedback";
import "./CaseExtensions.css";

type Extension = {version: string; enabled: string | boolean; settings?: Record<string, unknown>};
type ExtensionProps = {caseId: number; snapshot: CaseDictSnapshot; onSaved: () => Promise<void>};

const validNamespace = /^[a-z][a-z0-9._-]{0,63}$/;

function extensionDraft(namespace: string, entry?: Extension) {
  return {
    namespace,
    version: entry?.version ?? "",
    enabled: entry ? String(entry.enabled).toLowerCase() === "true" : true,
    settings: JSON.stringify(entry?.settings ?? {}, null, 2),
  };
}

function ExtensionEditor({caseId, snapshot, onSaved, namespace, entry, entries, onSelect}: ExtensionProps & {
  namespace: string;
  entry?: Extension;
  entries: Record<string, Extension>;
  onSelect: (name: string) => void;
}) {
  const creating = !namespace;
  const form = useCaseDraft(snapshot, extensionDraft(namespace, entry), namespace);
  const draft = form.draft.value;
  const edit = <K extends keyof typeof draft,>(key: K, value: (typeof draft)[K]) => form.edit({...draft, [key]: value});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const deleted = !creating && !entry;
  const names = Object.keys(entries).sort();

  const persist = async (remove = false) => {
    if (busyRef.current || deleted) return;
    const name = draft.namespace.trim();
    if (remove && !window.confirm(`删除扩展“${namespace}”？`)) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const submitted = form.capture();
      const route = `/cases/${caseId}/caseDict/extensions/${encodeURIComponent(name)}`;
      if (remove) {
        const query = new URLSearchParams({expected_revision: String(submitted.conditions.expected_revision),
          expected_sha256: submitted.conditions.expected_sha256});
        const receipt = await api.del<DictReceipt>(`${route}?${query}`);
        form.accept(submitted, receipt);
        await onSaved();
        onSelect("");
      } else {
        if (!validNamespace.test(name)) throw new Error("命名空间以小写字母开头，可使用数字、点、下划线和连字符，最多 64 个字符。");
        if (creating && Object.hasOwn(entries, name)) throw new Error("该命名空间已存在，请选择已有扩展进行编辑。");
        const version = draft.version;
        if (!version.trim()) throw new Error("请输入版本。");
        let settings: unknown;
        try { settings = JSON.parse(draft.settings); }
        catch { throw new Error("设置不是有效的 JSON。"); }
        if (settings === null || typeof settings !== "object" || Array.isArray(settings)) {
          throw new Error("扩展设置必须是 JSON 对象。");
        }
        const receipt = await api.put<DictReceipt>(route, {version, enabled: draft.enabled, settings, ...submitted.conditions});
        form.accept(submitted, receipt);
        await onSaved();
        if (creating) onSelect(name);
        setMessage("扩展已保存");
      }
    } catch (cause) {
      setError(draftError(cause));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const reviewedEntries = form.review?.parsed?.extensions?.entries ?? {};
  const reviewedNamespace = creating ? draft.namespace.trim() : namespace;
  const reviewed = Object.hasOwn(reviewedEntries, reviewedNamespace) ? reviewedEntries[reviewedNamespace] as Extension : undefined;
  return <form className="case-extensions-form" data-testid="case-extension-form" data-dirty={form.draft.dirty}
    onSubmit={event => {event.preventDefault(); void persist();}}>
    <label>选择扩展
      <select aria-label="选择扩展" disabled={busy} value={namespace} onChange={event => {
        if (form.isDirty() && !window.confirm("切换扩展会放弃此表单的本地草稿，是否继续？")) return;
        onSelect(event.target.value);
      }}>
        <option value="">注册新扩展</option>
        {deleted && <option value={namespace}>{namespace}（已删除）</option>}
        {names.map(name => <option key={name} value={name}>{name}</option>)}
      </select>
    </label>
    {(error || deleted) && <div role="alert">
      <p>{error || "服务器已删除此扩展，草稿已保留。"}</p>
      <button type="button" disabled={busy} onClick={async () => {
        try { await form.inspect(caseId); }
        catch (cause) { setError(draftError(cause)); }
      }}>查看服务器版本</button>
    </div>}
    {form.review && <DraftReview value={reviewed} allowKeep={creating ? !reviewed : !!reviewed} onChoose={keep => {
      form.resolve(extensionDraft(namespace, reviewed), keep);
      setError("");
      void onSaved();
    }} />}
    <fieldset disabled={busy || deleted}>
      {creating && <label>命名空间<input required value={draft.namespace} onChange={event => edit("namespace", event.target.value)} /></label>}
      <label>版本<input required value={draft.version} onChange={event => edit("version", event.target.value)} /></label>
      <label className="case-extension-state">
        <input type="checkbox" checked={draft.enabled} onChange={event => edit("enabled", event.target.checked)} /> 启用扩展
      </label>
      <label>设置（JSON 对象）<textarea aria-label="设置（JSON 对象）" rows={5}
        value={draft.settings} onChange={event => edit("settings", event.target.value)} /></label>
      <div className="case-extension-actions">
        <button type="submit" disabled={!form.draft.dirty}>{creating ? "注册扩展" : "保存扩展"}</button>
        {!creating && <button type="button" onClick={() => void persist(true)}>删除扩展</button>}
      </div>
    </fieldset>
    {message && <p role="status">{message}</p>}
  </form>;
}

export default function CaseExtensions(props: ExtensionProps) {
  const extensions = props.snapshot.parsed?.extensions;
  const entries = (extensions?.entries ?? {}) as Record<string, Extension>;
  const [selection, setSelection] = useState<string | null>(null);
  const namespace = selection ?? Object.keys(entries).sort()[0] ?? "";
  const entry = Object.hasOwn(entries, namespace) ? entries[namespace] : undefined;
  return <section className="case-extensions" aria-label="算例扩展">
    <h3>算例扩展</h3>
    <ExtensionEditor {...props} key={namespace} namespace={namespace} entry={entry}
      entries={entries} onSelect={setSelection} />
  </section>;
}
