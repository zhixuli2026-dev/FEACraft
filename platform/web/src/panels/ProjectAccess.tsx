import { useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import './ProjectAccess.css';

type Role = 'viewer' | 'member';
interface Grant { org_id: number; role: Role; role_ceiling: Role; enabled: boolean; delegated: boolean; version: number }
interface Status { owner: boolean; can_manage: boolean; grants: Grant[] }
interface Organization { id: number; name: string; role: string }
const read = (role: Role) => role === 'member' ? '可编辑' : '只读';

export default function ProjectAccess({ workspace, caseId }: { workspace: number; caseId?: number }) {
  const [scope, setScope] = useState<'workspace' | 'case'>('workspace');
  const [status, setStatus] = useState<Status | null>(null);
  const [memberships, setMemberships] = useState<Organization[]>([]);
  const [org, setOrg] = useState('');
  const [role, setRole] = useState<Role>('viewer');
  const [enabled, setEnabled] = useState(true);
  const [delegated, setDelegated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const target = scope==='case' && caseId ? `/cases/${caseId}/access-grants` : `/workspaces/${workspace}/access-grants`;
  const selected = status?.grants.find(item => String(item.org_id) === org);

  useEffect(() => {
    const controller = new AbortController();
    api.get<Organization[]>('/org/memberships', undefined, controller.signal).then(setMemberships).catch(() => {});
    return () => controller.abort();
  }, []);

  function selectGrant(value: string, snapshot = status) {
    const grant = snapshot?.grants.find(item => String(item.org_id) === value);
    setOrg(value);
    setRole(grant?.role ?? 'viewer');
    setDelegated(grant?.delegated ?? false);
    setEnabled(grant?.enabled ?? true);
  }

  useEffect(() => {
    const controller = new AbortController();
    setStatus(null);
    setError('');
    api.get<Status>(target, undefined, controller.signal).then(value => {
      setStatus(value);
      selectGrant(String(value.grants[0]?.org_id ?? ''),value);
    }).catch(failure => {
      if (controller.signal.aborted) return;
      setError(failure instanceof ApiError && failure.status===404
        ? '当前账户没有此范围的授权管理权限。' : String(failure));
    });
    return () => controller.abort();
  }, [target,reload]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!status || busy || !/^[1-9]\d{0,9}$/.test(org)) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const saved=await api.put<Grant>(`${target}/${org}`,{
        role,enabled,expected_version:selected?.version ?? 0,
        ...(status.owner ? {delegated} : {}),
      });
      setStatus({...status,grants:[...status.grants.filter(item => item.org_id !== saved.org_id),saved]
        .sort((a,b) => a.org_id-b.org_id)});
      setNotice(saved.enabled ? '授权已保存。' : '授权已撤销。');
    } catch (failure) {
      setError(String(failure));
    } finally {
      setBusy(false);
    }
  }

  return <section className="project-access" aria-label="组织授权" data-testid="project-access">
    <label>授权范围 <select aria-label="授权范围" disabled={busy} value={scope}
      onChange={event => { setNotice(''); setScope(event.target.value as 'workspace' | 'case'); }}>
      <option value="workspace">整个工作区</option>
      <option value="case" disabled={!caseId}>当前算例</option>
    </select></label>
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => setReload(n => n+1)}>重新读取</button></p>}
    {!status && !error && <p role="status">正在读取授权…</p>}
    {status && <form onSubmit={save}>
      {status.grants.length > 0 && <ul aria-label="已有授权">
        {status.grants.map(grant => <li key={grant.org_id}>
          <button type="button" disabled={busy} aria-pressed={org===String(grant.org_id)} onClick={() => selectGrant(String(grant.org_id))}>
            {memberships.find(item => item.id===grant.org_id)?.name ?? `组织 ${grant.org_id}`}
          </button>
          <span>{read(grant.role)} · {grant.enabled ? '已启用' : '已撤销'}</span>
        </li>)}
      </ul>}
      <label>组织编号 <input type="text" inputMode="numeric" aria-label="授权组织编号" value={org}
        list="project-access-member-orgs" readOnly={!status.owner} disabled={busy} onChange={event => selectGrant(event.target.value)} /></label>
      <datalist id="project-access-member-orgs">
        {memberships.map(item => <option key={item.id} value={item.id} label={item.name} />)}
      </datalist>
      <label>访问角色 <select aria-label="授权角色" disabled={busy} value={role}
        onChange={event => setRole(event.target.value as Role)}>
        <option value="viewer">只读</option>
        <option value="member" disabled={!status.owner && selected?.role_ceiling!=='member'}>可编辑</option>
      </select></label>
      {status.owner && <label><input type="checkbox" checked={delegated} disabled={busy}
        onChange={event => setDelegated(event.target.checked)} /> 允许该组织管理员管理此授权</label>}
      <label><input type="checkbox" checked={enabled} disabled={busy}
        onChange={event => setEnabled(event.target.checked)} /> 启用授权</label>
      <button type="submit" disabled={busy || !/^[1-9]\d{0,9}$/.test(org)}>{busy ? '保存中…' : '保存授权'}</button>
    </form>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
