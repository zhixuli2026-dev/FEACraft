import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import "./ScnetDetail.css";

type Row = Record<string, any>;
type Tab = "connect" | "jobs" | "files" | "containers" | "notebooks" | "desktops";
type DetailGroup = "overview" | "resources" | "advanced";
type DetailField = {key: string; label: string; value: string | number | boolean | null; group: DetailGroup};
type ResourceDetail = {summary: string; fields: DetailField[]; copy_text: string; truncated: boolean};
type RequestKind = "read" | "mutation" | "download" | "account";
type PendingRequest = {controller: AbortController; id: number; kind: RequestKind; received: boolean; scope: string};
type AccountRegion = {id: string; name: string; status: string; services: string[]; can_renew: boolean;
  token_expires_at: number | null};
type ScnetAccount = {id: string; revision: number; expires_at: number; account: Record<string, string>;
  notice: string | null; regions: AccountRegion[]};

const MUTATIONS: Record<string, string> = {
  "jobs.submit": "提交作业（可能产生费用）", "jobs.cancel": "终止作业",
  "files.mkdir": "新建目录", "files.upload": "上传文件", "files.rename": "重命名", "files.copy": "复制文件",
  "files.move": "移动文件", "files.delete": "删除文件",
  "containers.create": "创建容器（可能产生费用）", "containers.start": "启动容器（可能产生费用）",
  "containers.restart": "重启容器（可能产生费用）", "containers.stop": "停止容器",
  "containers.delete": "删除容器", "containers.exec": "在容器执行命令",
  "notebooks.create": "创建 Notebook（可能产生费用）", "notebooks.start": "开机（可能产生费用）",
  "notebooks.stop": "关机并保存环境", "notebooks.rename": "重命名 Notebook", "notebooks.release": "释放 Notebook",
  "desktops.create": "创建远程桌面（可能产生费用）", "desktops.delete": "删除远程桌面",
};

const DETAIL_GROUPS: [DetailGroup, string][] = [["overview", "基本信息"], ["resources", "资源与运行"],
  ["advanced", "其他字段"]];

const TABS: [Tab, string][] = [["connect", "连接与概览"], ["jobs", "计算作业"],
  ["files", "远程文件"], ["containers", "容器实例"], ["notebooks", "Notebook"], ["desktops", "远程桌面"]];
const SERVICE_LABELS: Record<string, string> = {hpc: "计算", ai: "AI", files: "文件", shell: "终端"};
const ACCOUNT_LABELS: [string, string][] = [["userName", "用户名"], ["fullName", "姓名"],
  ["accountName", "账户"], ["accountStatus", "状态"], ["accountBalance", "余额"], ["computerCenter", "算力中心"]];

export default function ScnetPanel() {
  const [tab, setTab] = useState<Tab>("connect");
  const [config, setConfig] = useState({base_url: "", access_key: "", secret_key: "", token: ""});
  const [username, setUsername] = useState("");
  const [account, setAccount] = useState<ScnetAccount | null>(null);
  const [region, setRegion] = useState("region-a");
  const [connected, setConnected] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [status, setStatus] = useState("");
  const [downloadText, setDownloadText] = useState("");
  const [requestKind, setRequestKind] = useState<RequestKind | null>(null);
  const busy = requestKind !== null;
  const currentRegion = account?.regions.find(item => item.id === region);
  const scopeFor = (resource: string) => JSON.stringify([account?.id ?? config.base_url.trim(), region, resource]);
  const credentials = account ? {session_id: account.id, revision: account.revision, region_id: region} : config;
  const [pendingScopes, setPendingScopes] = useState<Set<string>>(() => new Set());
  const needsRefresh = pendingScopes.has(scopeFor(tab));
  const mutationDisabled = busy || !connected || needsRefresh;
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [detail, setDetail] = useState<{model: ResourceDetail; request: number} | null>(null);
  const [historyJobs, setHistoryJobs] = useState(false);
  const [form, setForm] = useState({name: "demo", command: "echo SCNET_OK", path: "Ref/a.txt", content: "ABC", target: "Ref/b.txt"});
  const sequence = useRef(0);
  const active = useRef<PendingRequest | null>(null);
  const isCurrent = (request: PendingRequest): boolean => active.current === request;
  useEffect(() => () => {
    active.current?.controller.abort();
    active.current = null;
  }, []);

  const begin = (kind: RequestKind, scope: string) => {
    if (active.current) return null;
    const request = {kind, scope, id: ++sequence.current, controller: new AbortController(), received: false};
    active.current = request;
    setRequestKind(kind);
    return request;
  };

  const finish = (request: PendingRequest) => {
    if (!isCurrent(request)) return;
    active.current = null;
    setRequestKind(null);
  };

  const requireRefresh = (scope: string) => setPendingScopes(previous => new Set([...previous, scope]));

  const cancelRequest = () => {
    const request = active.current;
    if (!request) return;
    finish(request);
    request.controller.abort();
    if (request.kind === "mutation") requireRefresh(request.scope);
    setStatus(request.kind === "mutation"
      ? request.received ? "已收到远端变更结果。请刷新资源查看当前状态。" : "已停止等待，远端结果待确认。请刷新资源核对。"
      : request.kind === "download" ? "下载已取消" : "查询已取消");
    return request.kind;
  };

  const displayList = (result: unknown, operation: string) => {
    if (!Array.isArray(result)) throw new Error("未取得有效的资源列表，请刷新后重试");
    setRows(result);
    setPage(0);
    setHistoryJobs(operation === "jobs.history");
    if (operation.endsWith(".list")) {
      const scope = scopeFor(operation.split(".")[0]);
      setPendingScopes(previous => {
        if (!previous.has(scope)) return previous;
        const next = new Set(previous);
        next.delete(scope);
        return next;
      });
    }
  };

  const run = async (operation: string, args: Row = {}, openDetail = false,
                     options: {list?: boolean; connect?: boolean; refresh?: boolean} = {}) => {
    if (active.current) return;
    if (!connected && !options.connect) {
      setStatus("请先在连接与概览中连接 SCNET");
      return;
    }
    const label = MUTATIONS[operation];
    const scope = scopeFor(operation.split(".")[0]);
    if (label) {
      if (pendingScopes.has(scope)) {
        setStatus("请刷新资源，核对上一次远端结果后再执行变更。");
        return;
      }
      const target = args.resource?.name ?? args.job?.name ?? args.path ?? args.id;
      const destination = args.target ? ` → ${args.target}` : "";
      if (!window.confirm(`${label}${target == null ? "" : `「${String(target)}${destination}」`}？\n发送后请以远端资源状态为准。`)) {
        setStatus("已取消本次变更，请求发送次数为 0");
        return;
      }
    }
    const request = begin(label ? "mutation" : "read", scope);
    if (!request) return;
    if (openDetail) setDetail(null);
    setStatus(`${label || operation}…`);
    try {
      const result = await api.post<any>(`/scnet/${operation}`, {...credentials, args, confirmed: Boolean(label), detail_view: openDetail},
        undefined, request.controller.signal);
      if (!isCurrent(request)) return;
      request.received = true;
      if (options.list || options.connect) displayList(result, operation);
      if (options.connect) { setConnected(true); setTab("jobs"); }
      if (openDetail) setDetail({model: result, request: request.id});
      else if (operation.endsWith(".open")) window.open(result.url, "_blank", "noopener,noreferrer");
      if (operation === "files.download") setDownloadText(String(result ?? ""));
      if (options.refresh) {
        const list = `${operation.split(".")[0]}.list`;
        const refreshed = await api.post<unknown>(`/scnet/${list}`, credentials, undefined, request.controller.signal);
        if (!isCurrent(request)) return;
        displayList(refreshed, list);
      }
      setStatus(`${operation} 完成`);
    } catch (error: unknown) {
      if (!isCurrent(request)) return;
      const message = error instanceof Error ? error.message : String(error);
      if (label && request.received) {
        requireRefresh(request.scope);
        setStatus(`远端变更已响应，列表刷新失败。${message}`);
      }
      else if (label && (!(error instanceof ApiError) || error.status >= 500)) {
        requireRefresh(request.scope);
        setStatus(`${message}。远端结果待确认，请刷新资源核对。`);
      }
      else setStatus(message);
    } finally {
      finish(request);
    }
  };

  const download = async (path: string) => {
    if (!connected || !path.trim()) return;
    const request = begin("download", scopeFor("files"));
    if (!request) return;
    setStatus("正在下载远程文件…");
    try {
      const token = localStorage.getItem("vf_token");
      const response = await fetch("/api/v1/scnet/files:download", {method: "POST", signal: request.controller.signal,
        headers: {"Content-Type": "application/json", ...(token ? {Authorization: `Bearer ${token}`} : {})},
        body: JSON.stringify({...config, args: {path}})});
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new ApiError(response.status, detail.detail ?? response.statusText);
      }
      const blob = await response.blob();
      if (!isCurrent(request)) return;
      const encoded = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const name = encoded ? decodeURIComponent(encoded) : "download";
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = name; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
      setStatus("文件已交给浏览器保存");
    } catch (error: unknown) {
      if (isCurrent(request)) setStatus(String(error));
    } finally {
      finish(request);
    }
  };

  const connect = () => run("jobs.list", {}, false, {connect: true});
  const load = (operation: string) => run(operation, {}, false, {list: true});
  const runAndRefresh = (operation: string, args: Row = {}) => run(operation, args, false, {refresh: true});
  const accountAction = async <T,>(operation: string, args: Row, waiting: string, updated: (result: T) => void) => {
    const request = begin("account", scopeFor("connect"));
    if (!request) return;
    setStatus(waiting);
    try {
      const result = await api.post<T>(`/scnet/account:${operation}`, args, undefined, request.controller.signal);
      if (isCurrent(request)) updated(result);
    } catch (error: unknown) {
      if (isCurrent(request)) setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      finish(request);
    }
  };
  const clearCredentials = () => {
    setUsername("");
    setConfig(previous => ({...previous, access_key: "", secret_key: "", token: ""}));
  };
  const connectAccount = () => accountAction<ScnetAccount>("connect", {
    username: username.trim(), access_key: config.access_key, secret_key: config.secret_key,
  }, "正在验证 SCNET 账户并发现区域…", result => {
    const initial = result.regions.find(item => item.services.includes("hpc"))
      ?? result.regions.find(item => item.services.length) ?? result.regions[0];
    setAccount(result); setRegion(initial?.id ?? ""); setConnected(true);
    clearCredentials(); setRows([]); setDetail(null); setStatus("SCNET 账户已连接");
  });
  const switchTab = (next: Tab) => {
    if (next === tab || active.current?.kind === "account") return;
    const interrupted = cancelRequest();
    setDownloadText(""); setTab(next); setRows([]); setDetail(null); setQuery(""); setPage(0);
    if (next !== "connect" && connected && interrupted !== "mutation") void load(`${next}.list`);
  };
  const disconnect = () => {
    cancelRequest();
    setConnected(false); setRows([]); setDetail(null); setDownloadText(""); setQuery(""); setPage(0);
  };
  const updateConnection = (key: keyof typeof config, value: string) => {
    disconnect();
    setConfig({...config, [key]: value});
  };
  const switchRegion = (value: string) => {
    if (account) {
      cancelRequest();
      setRegion(value); setRows([]); setDetail(null); setDownloadText(""); setQuery(""); setPage(0); setStatus("");
      return;
    }
    disconnect();
    setRegion(value);
    setConfig({...config, token: ""});
    setStatus("已切换区域。请填写该区域的服务地址和令牌后连接。");
  };
  const logout = () => {
    if (account) {
      cancelRequest();
      void accountAction("logout", {session_id: account.id}, "正在注销 SCNET 会话…", () => {
        disconnect(); setAccount(null); setRegion("region-a"); clearCredentials(); setStatus("SCNET 会话已注销");
      });
      return;
    }
    disconnect();
    clearCredentials();
    setStatus("已注销，页面凭据已清除");
  };
  const filtered = useMemo(() => rows.filter((row) => JSON.stringify(row).toLowerCase().includes(query.toLowerCase())), [rows, query]);
  const visible = filtered.slice(page * 5, page * 5 + 5);

  return <div className="scnet-workbench" style={s.root}>
    <div style={{...s.tabs, flexWrap: account ? "wrap" : "nowrap"}}>{TABS.map(([id, label]) => <button key={id} data-testid={`scnet-tab-${id}`}
      aria-pressed={tab === id} style={tab === id ? s.tabOn : s.tab} disabled={requestKind === "account" || Boolean(account && id !== "connect" &&
        (id !== "jobs" && id !== "desktops" || !currentRegion?.services.includes("hpc")))}
      onClick={() => switchTab(id)}>{label}</button>)}</div>
    <div style={s.body}>
      {tab === "connect" && <section>
        <h3>连接与概览</h3>
        {account ? <>
          <dl className="scnet-account-summary">
            {ACCOUNT_LABELS.map(([key, label]) => account.account[key] && <div key={key}>
              <dt>{label}</dt><dd>{account.account[key]}</dd>
            </div>)}
          </dl>
          {account.notice && <p>{account.notice}</p>}
          <div className="scnet-account-grid">
            <label>区域<select aria-label="区域" style={s.input} value={region} disabled={!account.regions.length || busy}
              onChange={(e) => switchRegion(e.target.value)}>
              {!account.regions.length && <option value="">暂无授权区域</option>}
              {account.regions.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select></label>
            {currentRegion && <div className="scnet-region-status">
              <span>{currentRegion.status}</span>
              {currentRegion.services.length > 0 && <span>{currentRegion.services.map(service => SERVICE_LABELS[service]).join("、")}</span>}
              {currentRegion.token_expires_at !== null && <span>区域凭证到期 {new Date(currentRegion.token_expires_at * 1000).toLocaleString()}</span>}
            </div>}
          </div>
          <p style={s.note}>账户连接目前可查看计算作业和远程桌面。</p>
          <div style={s.toolbar}>
            <button style={s.button} disabled={!currentRegion?.can_renew || busy} onClick={() => accountAction<ScnetAccount>("renew", {
              session_id: account.id, revision: account.revision, region_id: region,
            }, "正在续约当前区域凭证…", result => {setAccount(result); setStatus("当前区域凭证已续约");})}>续约区域凭证</button>
            <button style={s.button} disabled={busy} onClick={logout}>注销</button>
          </div>
        </> : <>
        <div className="scnet-account-grid">
          <label>SCNET 用户名<input style={s.input} autoComplete="off" value={username} disabled={requestKind === "account"}
            onChange={(e) => setUsername(e.target.value)} /></label>
          <label>Access Key<input type="password" style={s.input} value={config.access_key} disabled={requestKind === "account"}
            onChange={(e) => updateConnection("access_key", e.target.value)} /></label>
          <label>Secret Key<input data-testid="scnet-secret" type="password" style={s.input} value={config.secret_key}
            disabled={requestKind === "account"} onChange={(e) => updateConnection("secret_key", e.target.value)} /></label>
        </div>
        <div style={s.toolbar}><button style={s.primary} onClick={connectAccount}
          disabled={busy || !username.trim() || !config.access_key || !config.secret_key}>连接 SCNET 账户</button></div>
        <div className="scnet-manual" role="group" aria-label="手动服务连接">
        <div className="scnet-account-grid">
          <label>服务地址<input data-testid="scnet-base-url" style={s.input} value={config.base_url}
            disabled={requestKind === "account"} onChange={(e) => updateConnection("base_url", e.target.value)} /></label>
          <label>区域<select aria-label="区域" style={s.input} value={region} disabled={requestKind === "account"} onChange={(e) => switchRegion(e.target.value)}>
            <option value="region-a">region-a</option><option value="region-b">region-b</option>
          </select></label>
          <label>会话令牌<input type="password" style={s.input} value={config.token}
            disabled={requestKind === "account"} onChange={(e) => updateConnection("token", e.target.value)} /></label>
        </div>
        <div style={s.toolbar}><button data-testid="scnet-connect" style={s.button}
          disabled={busy || !config.base_url.trim() || !config.access_key || !config.secret_key} onClick={connect}>手动连接</button>
        <button style={s.button} disabled={requestKind === "account"} onClick={logout}>注销</button></div>
        </div></>}
      </section>}
      {tab !== "connect" && <>
        <div style={s.toolbar}>
          <input data-testid="scnet-search" style={s.input} placeholder="搜索当前资源" value={query}
            onChange={(e) => {setQuery(e.target.value); setPage(0);}} />
          <button style={s.button} disabled={busy || !connected}
            onClick={() => load(tab === "jobs" && historyJobs ? "jobs.history" : `${tab}.list`)}>刷新</button>
          {!account && tab === "jobs" && <button style={s.button} disabled={busy || !connected} onClick={() => load(historyJobs ? "jobs.list" : "jobs.history")}>
            {historyJobs ? "实时作业" : "历史"}</button>}
          {!account && tab !== "jobs" && tab !== "files" && <button data-testid="scnet-create" style={s.primary} disabled={mutationDisabled}
            onClick={() => runAndRefresh(`${tab}.create`, {resource: {name: form.name}})}>手动创建</button>}
        </div>
        {!account && tab === "jobs" && <div style={s.toolbar}>
          <input style={s.input} value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} />
          <input style={s.input} value={form.command} onChange={(e) => setForm({...form, command: e.target.value})} />
          <button data-testid="scnet-job-submit" style={s.primary} disabled={mutationDisabled}
            onClick={() => runAndRefresh("jobs.submit", {job: {name: form.name, command: form.command}})}>提交 BASIC 作业</button>
        </div>}
        {tab === "files" && <div data-testid="file-actions" style={s.formGrid}>
          <input style={s.input} value={form.path} onChange={(e) => setForm({...form, path: e.target.value})} />
          <input style={s.input} value={form.target} onChange={(e) => setForm({...form, target: e.target.value})} />
          <input style={s.input} value={form.content} onChange={(e) => setForm({...form, content: e.target.value})} />
          <div style={s.toolbar}>
            <button style={s.button} disabled={mutationDisabled} onClick={() => runAndRefresh("files.mkdir", {path: form.path})}>新建目录</button>
            <button style={s.button} disabled={mutationDisabled} onClick={() => runAndRefresh("files.upload", {path: form.path, content: form.content})}>上传</button>
            <button style={s.button} disabled={mutationDisabled} onClick={() => runAndRefresh("files.rename", {path: form.path, target: form.target})}>重命名</button>
            <button style={s.button} disabled={mutationDisabled} onClick={() => runAndRefresh("files.copy", {path: form.path, target: form.target})}>复制</button>
            <button style={s.button} disabled={mutationDisabled} onClick={() => runAndRefresh("files.move", {path: form.path, target: form.target})}>移动</button>
            <button style={s.button} disabled={busy || !connected} onClick={() => download(form.path)}>下载</button>
            <button style={s.button} disabled={busy || !connected || form.path.endsWith("/")} onClick={() => run("files.download", {path: form.path})}>预览文本</button>
            <button style={s.danger} disabled={mutationDisabled} onClick={() => runAndRefresh("files.delete", {path: form.path})}>删除</button>
          </div>
        </div>}
        <div style={s.table} data-testid="scnet-table">
          {visible.map((row, index) => {
            const id = tab === "desktops" ? row.sessionId ?? row.id : row.id ?? row.jobId ?? row.notebookId;
            const name = row.name ?? row.jobName ?? row.instanceServiceName ?? row.notebookName ?? row.path ?? id;
            return <div key={`${id ?? row.path ?? index}`} style={s.row}>
            <span style={{flex: 1, minWidth: 0, overflowWrap: "anywhere"}}>{name}</span>
            <code>{row.status ?? row.jobStatus ?? row.jobState ?? row.notebookStatus ?? ""}</code>
            {tab === "jobs" && <>
              <button style={s.small} disabled={busy || id == null} onClick={() => historyJobs
                ? run("jobs.history-detail", {id, manager_id: row.jobmanagerId, account_time: row.acctTime}, true)
                : run("jobs.detail", {id}, true)}>详情</button>
              {!account && !historyJobs && <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("jobs.cancel", {id})}>终止</button>}</>}
            {tab === "files" && <button style={s.small} disabled={busy} onClick={() => download(row.path)}>下载</button>}
            {tab !== "jobs" && tab !== "files" && <>
              <button style={s.small} disabled={busy || id == null} onClick={() => run(`${tab}.detail`, {id}, true)}>详情</button>
              {!account && <button style={s.small} disabled={busy || id == null} onClick={() => run(`${tab}.open`, {id})}>打开</button>}
              {tab === "containers" && <>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("containers.start", {id})}>启动</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("containers.restart", {id})}>重启</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => run("containers.exec", {id, command: form.command})}>执行</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("containers.stop", {id})}>停止</button></>}
              {tab === "notebooks" && <>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("notebooks.start", {id})}>开机</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("notebooks.stop", {id, save_environment: true})}>关机存环境</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("notebooks.rename", {id, name: form.name})}>重命名</button>
                <button style={s.small} disabled={mutationDisabled || id == null} onClick={() => runAndRefresh("notebooks.release", {id})}>释放</button></>}
              {!account && tab !== "notebooks" && <button style={s.danger} disabled={mutationDisabled || id == null}
                onClick={() => runAndRefresh(`${tab}.delete`, {id})}>删除</button>}</>}
          </div>})}
          {!visible.length && <div style={s.note}>当前页没有资源</div>}
        </div>
        <div style={s.toolbar}>
          <button style={s.small} disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
          <span>第 {page + 1} 页 / 共 {Math.max(1, Math.ceil(filtered.length / 5))} 页</span>
          <button style={s.small} disabled={(page + 1) * 5 >= filtered.length} onClick={() => setPage(page + 1)}>下一页</button>
        </div>
      </>}
      <div data-testid="scnet-status" role="status" style={s.status}>{status}</div>
      {busy && requestKind !== "account" && <button type="button" style={s.button} onClick={cancelRequest}>{requestKind === "download" ? "取消下载" : "停止等待"}</button>}
      {downloadText && <pre data-testid="scnet-download-result" style={s.download}>{downloadText}</pre>}
      {detail && <DetailPanel key={detail.request} detail={detail.model} onClose={() => setDetail(null)} />}
    </div>
  </div>;
}

function DetailPanel({detail, onClose}: {detail: ResourceDetail; onClose: () => void}) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState({overview: true, resources: true, advanced: false});
  const [copyStatus, setCopyStatus] = useState("");
  const term = query.trim().toLocaleLowerCase();
  const fields = detail.fields.filter(field => !term || [field.label, field.key, field.value]
    .some(value => String(value ?? "").toLocaleLowerCase().includes(term)));
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus("已复制");
    } catch {
      setCopyStatus("复制失败，可选中文本手动复制");
    }
  };
  return <aside data-testid="scnet-detail" className="scnet-detail" aria-labelledby={`${id}-title`}>
    <header className="scnet-detail-header">
      <h3 id={`${id}-title`}>{detail.summary}</h3>
      <div className="scnet-detail-actions">
        <button type="button" style={s.button} onClick={() => void copy(detail.copy_text)}>复制摘要</button>
        <button type="button" style={s.button} onClick={onClose}>关闭详情</button>
      </div>
    </header>
    <input style={s.input} className="scnet-detail-search" type="search" aria-label="搜索详情字段"
      placeholder="搜索字段名称或内容" value={query} onChange={event => setQuery(event.target.value)} />
    {fields.length === 0 && <p className="scnet-detail-note">{term ? "没有匹配的字段" : "没有可显示的详情字段"}</p>}
    {DETAIL_GROUPS.map(([group, label]) => {
      const groupFields = fields.filter(field => field.group === group);
      if (groupFields.length === 0) return null;
      const expanded = Boolean(term) || open[group];
      return <section key={group} className="scnet-detail-group">
        <h4><button type="button" aria-expanded={expanded} aria-controls={`${id}-${group}`}
          onClick={() => setOpen({...open, [group]: !open[group]})} disabled={Boolean(term)}>
          {label}<span>{expanded ? "收起" : "展开"}</span>
        </button></h4>
        <dl id={`${id}-${group}`} hidden={!expanded}>
          {groupFields.map(({key, label: fieldLabel, value}, index) => {
            const display = value === "***" ? "已遮蔽" : value == null || value === "" ? "未提供" : String(value);
            const canCopy = value !== "***" && value != null && value !== "";
            return <div className="scnet-detail-field" key={`${key}-${index}`}>
              <dt title={key}>{fieldLabel}</dt>
              <dd>{display}</dd>
              {canCopy && <button type="button" aria-label={`复制 ${fieldLabel}`} style={s.small}
                onClick={() => void copy(String(value))}>复制</button>}
            </div>;
          })}
        </dl>
      </section>;
    })}
    {detail.truncated && <p className="scnet-detail-note">仅显示前 100 个字段。</p>}
    <div className="scnet-detail-note" role="status">{copyStatus}</div>
  </aside>;
}

const s: Record<string, React.CSSProperties> = {
  root: {height: "100%", display: "flex", flexDirection: "column", fontSize: 12, color: "#d7dce5"},
  tabs: {display: "flex", gap: 3, padding: 5, borderBottom: "1px solid #2a2f3a", overflow: "auto"},
  tab: {padding: "4px 8px", background: "transparent", color: "#8b93a7", border: "none", cursor: "pointer", whiteSpace: "nowrap"},
  tabOn: {padding: "4px 8px", background: "#1c2740", color: "#7db4ff", border: "none", borderRadius: 4, whiteSpace: "nowrap"},
  body: {flex: 1, overflow: "auto", padding: 10},
  formGrid: {display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 5, marginBottom: 8},
  toolbar: {display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", margin: "6px 0"},
  input: {minWidth: 0, flex: 1, padding: "5px 7px", background: "#0f1115", color: "#e8eaf0", border: "1px solid #2a2f3a", borderRadius: 5},
  button: {padding: "4px 9px", background: "#1a1e27", color: "#9ab", border: "1px solid #2a2f3a", borderRadius: 5, cursor: "pointer"},
  primary: {padding: "4px 10px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 5, cursor: "pointer"},
  small: {padding: "2px 6px", background: "#1a1e27", color: "#9ab", border: "1px solid #2a2f3a", borderRadius: 4, cursor: "pointer", fontSize: 11},
  danger: {padding: "2px 6px", background: "#7f1d1d", color: "#fca5a5", border: "1px solid #991b1b", borderRadius: 4, cursor: "pointer", fontSize: 11},
  table: {border: "1px solid #2a2f3a", borderRadius: 5},
  row: {display: "flex", alignItems: "center", gap: 5, padding: 6, borderBottom: "1px solid #1a1e27"},
  status: {padding: "6px 0", color: "#8b93a7"},
  note: {color: "#8b93a7", fontSize: 11},
  download: {padding: 8, background: "#0b0d12", color: "#9ab3d0", whiteSpace: "pre-wrap", borderRadius: 5},
};
