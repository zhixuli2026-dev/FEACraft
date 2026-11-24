import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import AccountSettings from "../components/AccountSettings";
import CliContracts from "../components/CliContracts";
import { activatePanel } from "../shell/dock";

interface Cap {
  python: { version: string; packages: Record<string, string | null> };
  database: { ok: boolean; url?: string; error?: string };
  storage: { backend: string; fs_root: string };
  binaries: { pandoc: string | null };
  wsl: { distro: string; reachable: boolean; openfoam?: boolean; wmake?: boolean;
         latexmk?: boolean; gmsh?: boolean; error?: string };
  stage_checks?: Record<string, { ok: boolean; missing: string[] }>;
}

/** 设置面板（骨架第一页：运行时能力自检——原版 cae.* 依赖注册表/Setup Doctor 的服务端等价） */
export default function SettingsPanel() {
  const { user, pathFormat, setPathFormat, theme, setTheme } = useStore();
  const [cap, setCap] = useState<Cap | null>(null);
  const [err, setErr] = useState("");
  const [at, setAt] = useState("");
  const [setup, setSetup] = useState<Record<string, string>>({});
  const [setupError, setSetupError] = useState("");

  const load = async () => {
    setErr("");
    try {
      setCap(await api.get<Cap>("/runtime/capabilities"));
      setAt(new Date().toLocaleTimeString());
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    }
  };
  const loadSetup = async () => {
    try { setSetup(await api.get<Record<string, string>>("/runtime/config")); }
    catch (error) { setSetupError(String(error)); }
  };
  const saveSetup = async () => {
    setSetupError("");
    try {
      setSetup(await api.patch<Record<string, string>>("/runtime/config", setup));
      await load();
    } catch (error) { setSetupError(String(error)); }
  };
  useEffect(() => { load(); loadSetup(); }, []);

  const ok = (v: boolean | null | undefined) =>
    v == null ? <span style={{ color: "#667" }}>缺</span>
      : v ? <span style={{ color: "#16a34a" }}>✓</span>
      : <span style={{ color: "#ef4444" }}>✕</span>;

  return (
    <div style={{ height: "100%", overflow: "auto", padding: 12, fontSize: 13, color: "#d7dce5" }}>
      <AccountSettings />
      <button type="button" style={s.btn} onClick={() => activatePanel("providers")}>管理模型配置</button>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <b>设置 · 运行时能力自检</b>
        <button style={s.btn} onClick={load}>重新探测</button>
        <span style={{ color: "#667", fontSize: 11 }}>{at}</span>
      </div>
      <div style={{ color: "#667", fontSize: 11, margin: "4px 0 10px" }}>
        当前用户：{user?.email ?? "?"} · 能力与依赖在服务端沙箱内（B/S 形态无需客户端装求解器）
      </div>
      <div style={s.h}>界面与路径</div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, marginBottom: 8 }}>
        全局外观
        <select data-testid="app-theme" title="全局 Light/Dark 主题" value={theme}
                onChange={(e) => setTheme(e.target.value as "dark" | "light")}
                style={{ background: "#10141c", color: "#d7dce5", border: "1px solid #2a2f3a", borderRadius: 4, padding: 4 }}>
          <option value="dark">Dark / 深色</option>
          <option value="light">Light / 浅色</option>
        </select>
      </label>
      <div style={s.h}>运行时 Setup（组织 admin）</div>
      {([["python", "Python 可执行文件"], ["pandoc", "pandoc 可执行文件"],
         ["wsl_distro", "WSL 发行版"], ["openfoam_bashrc", "OpenFOAM bashrc"]] as const).map(([key, label]) => (
        <label key={key} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, marginBottom: 5 }}>
          <span style={{ minWidth: 120 }}>{label}</span>
          <input data-testid={`setup-${key}`} value={setup[key] ?? ""} onChange={(event) =>
            setSetup({ ...setup, [key]: event.target.value })}
            style={{ flex: 1, minWidth: 0, background: "#10141c", color: "#d7dce5",
                     border: "1px solid #2a2f3a", borderRadius: 4, padding: 4 }} />
        </label>
      ))}
      <button data-testid="setup-save" style={s.btn} onClick={saveSetup}>保存 Setup 并重新探测</button>
      {setupError && <div role="alert" style={{ color: "#ef4444", fontSize: 11 }}>{setupError}</div>}
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
        路径文本格式
        <select title="路径文本格式" value={pathFormat}
                onChange={(e) => setPathFormat(e.target.value as "windows" | "linux")}
                style={{ background: "#10141c", color: "#d7dce5", border: "1px solid #2a2f3a", borderRadius: 4, padding: 4 }}>
          <option value="linux">Linux / POSIX（/）</option>
          <option value="windows">Windows（\）</option>
        </select>
      </label>
      <div style={{ color: "#667", fontSize: 11, margin: "4px 0 10px" }}>
        仅影响复制和显示；canonical storage key 与 @path 引用始终使用 POSIX `/`。
      </div>
      {err && <div style={{ color: "#ef4444" }}>{err}</div>}
      <QuotaSection />
      {!cap ? <div style={{ color: "#8b93a7" }}>探测中（WSL 探测约数秒）…</div> : (
        <>
          <div style={s.h}>Python 沙箱（server venv {cap.python.version}）</div>
          <div style={s.grid}>
            {Object.entries(cap.python.packages).map(([k, v]) => (
              <div key={k} style={s.cell}>{ok(v != null)} {k} <span style={{ color: "#667" }}>{v ?? ""}</span></div>
            ))}
          </div>
          <div style={s.h}>数据层</div>
          <div style={s.grid}>
            <div style={s.cell}>{ok(cap.database.ok)} Postgres <span style={{ color: "#667" }}>{cap.database.url ?? cap.database.error}</span></div>
            <div style={s.cell}>{ok(true)} 存储 <span style={{ color: "#667" }}>{cap.storage.backend} · {cap.storage.fs_root}</span></div>
            <div style={s.cell}>{ok(!!cap.binaries.pandoc)} pandoc <span style={{ color: "#667" }}>{cap.binaries.pandoc ?? "未找到"}</span></div>
          </div>
          <div style={s.h}>WSL Linux 沙箱（{cap.wsl.distro}）</div>
          {!cap.wsl.reachable ? (
            <div style={{ color: "#ef4444", fontSize: 12 }}>不可达：{cap.wsl.error ?? ""}</div>
          ) : (
            <div style={s.grid}>
              <div style={s.cell}>{ok(cap.wsl.openfoam)} OpenFOAM（blockMesh）</div>
              <div style={s.cell}>{ok(cap.wsl.wmake)} wmake（UDF 编译链）</div>
              <div style={s.cell}>{ok(cap.wsl.latexmk)} latexmk（TeX Live）</div>
              <div style={s.cell}>{ok(cap.wsl.gmsh)} gmsh</div>
            </div>
          )}
          {cap.stage_checks && (
            <>
              <div style={s.h}>阶段最小能力</div>
              <div data-testid="stage-checks" style={{ display: "flex", gap: 6, flexWrap: "wrap", fontSize: 11 }}>
                {Object.entries(cap.stage_checks).map(([stage, check]) => (
                  <span key={stage} style={{ ...s.cell,
                    color: check.ok ? "#16a34a" : "#ef4444" }}>
                    {check.ok ? "✓" : "✕"} {stage} {check.ok ? "就绪" : `缺 ${check.missing.join(", ")}`}
                  </span>
                ))}
              </div>
            </>
          )}
          <div style={{ color: "#667", fontSize: 11, marginTop: 10 }}>
            全局 Light/Dark 会同步面板、Monaco、终端和 CAD；字号等个性化设置后续加入此页。
          </div>

          <MonitorSection />
          <CliContracts />
        </>
      )}
    </div>
  );
}

/** 配额、邀请与组织 admin 成员控制台 */
function QuotaSection() {
  const { user } = useStore();
  const [org, setOrg] = useState<any>(null);
  const [directory, setDirectory] = useState<any>(null);
  const [invitations, setInvitations] = useState<any[]>([]);
  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [memberError, setMemberError] = useState("");
  const [auditData, setAuditData] = useState<any>(null);
  const [auditAction, setAuditAction] = useState("");
  const [plans, setPlans] = useState<any[]>([]);
  const [billing, setBilling] = useState<any>(null);
  const load = async () => {
    const mine = await api.get<any>("/org/mine");
    setOrg(mine);
    setInvitations(await api.get<any[]>("/org/invitations"));
    const own = mine.members?.find((item: any) => item.user_id === user?.id);
    const adminDirectory = own?.role === "admin" ? await api.get<any>(`/org/${mine.org.id}/members`) : null;
    setDirectory(adminDirectory);
    setPlans(await api.get<any[]>("/org/plans"));
    setBilling(adminDirectory ? await api.get(`/org/${mine.org.id}/billing`) : null);
  };
  useEffect(() => { load().catch((error) => setMemberError(String(error))); }, []);
  const run = async (action: () => Promise<unknown>) => {
    setMemberError("");
    try { await action(); setEmail(""); await load(); }
    catch (error: any) { setMemberError(String(error?.message ?? error)); }
  };
  const loadAudit = async (action = auditAction) => {
    if (!org?.org?.id) return;
    setMemberError("");
    try {
      const query = action ? `?action=${encodeURIComponent(action)}` : "";
      setAuditData(await api.get(`/org/${org.org.id}/audit${query}`));
    } catch (error: any) { setMemberError(String(error?.message ?? error)); }
  };
  if (!org) return null;
  const u = org.usage ?? {};
  const q = u.quotas ?? {};
  const reset = u.llm_resets_at ? new Date(u.llm_resets_at) : null;
  const resetLabel = reset && Number.isFinite(reset.getTime())
    ? new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(reset)
    : null;
  const reserved = u.storage_bytes_reserved ?? 0;
  const reservedLabel = reserved >= 1024 * 1024
    ? `${(reserved / (1024 * 1024)).toFixed(2)} MiB`
    : `${reserved.toLocaleString("zh-CN")} B`;
  const license = org.offline_license;
  const licenseLabels: Record<string, string> = {
    active:"已生效", invalid:"校验失败", unassigned:"本组织未获授权", stale_revocations:"待更新撤销清单",
    revoked:"已撤销", not_yet_valid:"尚未生效", expired:"已到期", device_unavailable:"无法核对设备",
    other_device:"设备不匹配",
  };
  const licenseExpiry = license?.expires_at ? new Date(license.expires_at) : null;
  const rows: [string, number, number, string?][] = [
    ["并发作业", u.occupied_job_slots ?? u.running_jobs ?? 0, q.job_concurrent ?? 0],
    ["今日 LLM 请求", u.llm_msgs_today ?? 0, q.llm_msgs_per_day ?? 0, resetLabel ? `${resetLabel} 重置` : undefined],
    ["存储（MiB）", u.storage_mb_used ?? 0, q.storage_mb ?? 0, reserved > 0 ? `另有 ${reservedLabel} 已预留` : undefined],
  ];
  if (typeof u.occupied_viz_slots === "number") {
    rows.push(["并发可视化会话", u.occupied_viz_slots, q.viz_concurrent ?? 0]);
  }
  return (
    <>
      <div id="quota-heading" style={{ ...s.h, marginTop: 14 }}>订阅与配额（{org.org?.name} · {org.plan?.name ?? "无套餐"}）</div>
      {license && <p role="status" data-testid="offline-license-status" style={{ marginBlock: "3px 8px", color: "var(--vf-text)" }}>
        离线许可 · {licenseLabels[license.status] ?? "状态待确认"}
        {licenseExpiry && Number.isFinite(licenseExpiry.getTime()) &&
          ` · 到期 ${licenseExpiry.toLocaleDateString("zh-CN")}`}
      </p>}
      <section aria-labelledby="quota-heading" data-testid="effective-quotas" style={{ ...s.grid, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 130px), 1fr))" }}>
        {rows.map(([label, used, cap, detail]) => (
          <div key={label} style={s.cell}>
            <div style={{ fontSize: 12 }}>{label}</div>
            <div style={{ fontSize: 11, color: "#9ab3d0" }}>
              {used} / {cap}
              <div aria-hidden="true" style={{ height: 4, background: "#1a1e27", borderRadius: 2, marginTop: 4 }}>
                <div style={{ height: "100%", borderRadius: 2,
                  width: cap ? `${Math.min(100, used / cap * 100)}%` : "0%",
                  background: cap && used / cap > 0.85 ? "#ef4444" : "#2563eb" }} />
              </div>
              {detail && <div style={{ marginTop: 4 }}>{detail}</div>}
            </div>
          </div>
        ))}
      </section>
      {directory && billing && billing.provider !== "offline" && (
        <div data-testid="billing-console" style={{ marginTop: 7 }}>
          <div style={{ fontSize: 11, color: "#9ab3d0", marginBottom: 4 }}>
            Mock 支付（不扣款）· <span data-testid="billing-current">{billing.current?.plan_code ?? "无生效订阅"}</span>
            {billing.current?.expires_at ? ` · ${new Date(billing.current.expires_at).toLocaleDateString()}` : ""}
          </div>
          <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
            {plans.map((plan) => (
              <button data-testid={`plan-${plan.code}`} key={plan.code} style={s.btn}
                      onClick={() => run(() => api.post(`/org/${org.org.id}/billing:mock-checkout`,
                        {plan_code: plan.code, period: "monthly", confirmed: true}))}>
                {plan.code === "free" ? "降级 free" : `切换 ${plan.code}`} · {plan.price_cents / 100} 元/月
              </button>
            ))}
            {billing.current && <>
              <button data-testid="billing-renew" style={s.btn}
                      onClick={() => run(() => api.post(`/org/${org.org.id}/billing:renew`, {period: "monthly"}))}>续费</button>
              <button data-testid="billing-cancel" style={s.btn}
                      onClick={() => run(() => api.post(`/org/${org.org.id}/billing:cancel`, {}))}>取消订阅</button>
            </>}
          </div>
        </div>
      )}
      {invitations.length > 0 && (
        <div data-testid="org-invitations" style={{ marginTop: 8 }}>
          <div style={s.h}>待接受邀请</div>
          {invitations.map((item) => (
            <div key={item.org_id} style={s.memberRow}>
              <span style={{ flex: 1 }}>{item.org_name} · {item.role}</span>
              <button data-testid={`org-accept-${item.org_id}`} style={s.btn}
                      onClick={() => run(() => api.post(`/org/${item.org_id}/invitation:accept`, {}))}>接受</button>
              <button style={s.btn}
                      onClick={() => run(() => api.post(`/org/${item.org_id}/invitation:decline`, {}))}>拒绝</button>
            </div>
          ))}
        </div>
      )}
      {directory && (
        <div data-testid="org-admin" style={{ marginTop: 8 }}>
          <div style={s.h}>组织成员与 seat</div>
          <div data-testid="org-seats" style={{ fontSize: 11, color: "#9ab3d0", marginBottom: 5 }}>
            {directory.seats.used} / {directory.seats.limit} 已占用（active {directory.seats.active} · 邀请 {directory.seats.invited}）
          </div>
          <div style={{ display: "flex", gap: 5, marginBottom: 6 }}>
            <input data-testid="org-invite-email" placeholder="已注册用户邮箱" value={email}
                   onChange={(event) => setEmail(event.target.value)} style={s.memberInput} />
            <select aria-label="邀请角色" value={inviteRole} onChange={(event) => setInviteRole(event.target.value)} style={s.btn}>
              <option value="member">member</option><option value="viewer">viewer</option><option value="admin">admin</option>
            </select>
            <button data-testid="org-invite-submit" style={s.btn}
                    onClick={() => run(() => api.post(`/org/${org.org.id}/members`, {email, role: inviteRole}))}>邀请</button>
          </div>
          {directory.members.map((member: any) => (
            <div data-testid={`org-member-${member.user_id}`} key={member.user_id} style={s.memberRow}>
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }} title={member.email}>
                {member.name || member.email} · {member.status}
              </span>
              {member.user_id === user?.id ? <span style={{ color: "#667" }}>当前 {member.role}</span> :
                member.status === "invited" ? (
                  <button style={s.btn} onClick={() => run(() => api.del(`/org/${org.org.id}/members/${member.user_id}`))}>撤销邀请</button>
                ) : (
                  <select aria-label={`成员角色 ${member.user_id}`} value={member.status === "disabled" ? "disabled" : member.role}
                          onChange={(event) => run(() => api.patch(`/org/${org.org.id}/members/${member.user_id}`, {role: event.target.value}))}
                          style={s.btn}>
                    <option value="admin">admin</option><option value="member">member</option>
                    <option value="viewer">viewer</option><option value="disabled">disabled</option>
                  </select>
                )}
            </div>
          ))}
          <div style={s.h}>审计中心</div>
          <div style={{ display: "flex", gap: 5, marginBottom: 5 }}>
            <select aria-label="审计动作过滤" value={auditAction}
                    onChange={(event) => { setAuditAction(event.target.value); loadAudit(event.target.value); }} style={s.btn}>
              <option value="">全部动作</option>
              <option value="org.member.invited">成员邀请</option>
              <option value="org.member.accepted">接受邀请</option>
              <option value="org.member.role_changed">角色变更</option>
              <option value="job.submitted">作业提交</option>
              <option value="runtime.config.updated">Setup 变更</option>
            </select>
            <button data-testid="org-audit-load" style={s.btn} onClick={() => loadAudit()}>加载/刷新审计</button>
          </div>
          {auditData && (
            <div data-testid="org-audit">
              <div style={{ fontSize: 11, color: auditData.chain_valid ? "#16a34a" : "#ef4444" }}>
                {auditData.chain_valid ? "✓ 哈希链有效" : "✕ 哈希链损坏"} · {auditData.total} 条
              </div>
              {auditData.events.map((event: any) => (
                <div data-testid="org-audit-event" key={event.event_id} style={s.auditRow}
                     title={JSON.stringify(event.detail)}>
                  <span>{new Date(event.created_at).toLocaleString()}</span>
                  <b>{event.action}</b>
                  <span>actor {event.actor_user_id} · {event.resource_type} {event.resource_id}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {memberError && <div role="alert" style={{ color: "#ef4444", fontSize: 11 }}>{memberError}</div>}
      <div style={{ fontSize: 11, color: "#667", marginTop: 4 }}>
        邀请预留 seat，禁用释放 seat；支付与套餐升级归订阅生命周期。
      </div>
    </>
  );
}

/** J20：单进程运行时监控与告警 */
function MonitorSection() {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");
  const load = async () => {
    try { setData(await api.get("/runtime/monitor")); setError(""); }
    catch (reason: any) { setError(String(reason?.message ?? reason)); }
  };
  useEffect(() => { load(); }, []);
  return (
    <>
      <div style={{ ...s.h, marginTop: 14 }}>运行时监控
        <button data-testid="monitor-refresh" style={{ ...s.btn, marginLeft: 8 }} onClick={load}>刷新</button>
      </div>
      {error && <div role="alert" style={{ color: "#ef4444", fontSize: 11 }}>{error}</div>}
      {data && (
        <div data-testid="runtime-monitor" style={s.grid}>
          <div style={s.cell}>数据库 {data.database.ok ? "✓" : "✕"} · {data.database.latency_ms} ms</div>
          <div style={s.cell}>存储 {data.storage.ok ? "✓" : "✕"} · {data.storage.latency_ms} ms</div>
          <div style={s.cell}>作业：{data.jobs.running ?? 0} 运行 · {data.jobs.queued ?? 0} 排队</div>
          <div style={s.cell}>活动进程：{Object.values(data.processes).reduce((sum: number, value: any) => sum + value, 0)}</div>
          <div style={{ ...s.cell, color: data.alerts.length ? "#ef4444" : "#16a34a" }}>
            {data.alerts.length ? data.alerts.map((item: any) => item.code).join(", ") : "✓ 无当前告警"}
          </div>
        </div>
      )}
    </>
  );
}

const s: Record<string, React.CSSProperties> = {
  h: { fontSize: 11, color: "#667", textTransform: "uppercase", margin: "10px 0 4px" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 4 },
  cell: { background: "#10141c", border: "1px solid #1a1e27", borderRadius: 6,
          padding: "6px 10px", fontSize: 12 },
  memberRow: { display: "flex", alignItems: "center", gap: 6, padding: "4px 0", fontSize: 11, color: "#9ab3d0" },
  memberInput: { flex: 1, minWidth: 0, background: "#10141c", color: "#d7dce5", border: "1px solid #2a2f3a", borderRadius: 6, padding: "4px 7px", fontSize: 11 },
  auditRow: { display: "flex", flexDirection: "column", gap: 2, padding: "5px 0", borderBottom: "1px solid #1a1e27", fontSize: 10, color: "#8b93a7" },
  btn: { padding: "3px 12px", background: "#1a1e27", color: "#9ab",
         border: "1px solid #2a2f3a", borderRadius: 6, cursor: "pointer", fontSize: 12 },
};
