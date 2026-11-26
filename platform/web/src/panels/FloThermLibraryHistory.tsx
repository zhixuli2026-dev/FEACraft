import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api";
import { activatePanel } from "../shell/dock";
import { useStore } from "../store";

interface Version {
  id: string;
  job_id: number;
  generation: number;
  published_at: string;
  current: boolean;
}

interface PublishedFile {
  path: string;
  role: "library" | "build_log" | "load_log";
  sha256: string;
  size: number;
}

interface Publication extends Version {
  destination_dir: string;
  openfoam: string;
  files: PublishedFile[];
}

interface VersionPage {
  items: Version[];
  next: string | null;
}
type ReadState<T> = { key: string; data?: T; error?: string };

const fileLabels = { library: "下载辅助库", build_log: "下载编译日志", load_log: "下载加载日志" };
const publishDate = new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" });

export default function FloThermLibraryHistory({ caseId, latestJob }: { caseId: number; latestJob?: number }) {
  const endpoint = `/cases/${caseId}/flotherm/libraries`;
  const picker = useId();
  const [versions, setVersions] = useState<ReadState<VersionPage> | null>(null);
  const [before, setBefore] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [selection, setSelection] = useState("");
  const [details, setDetails] = useState<ReadState<Publication> | null>(null);
  const [downloadState, setDownloadState] = useState<{ key: string; path?: string; error?: string } | null>(null);
  const download = useRef<AbortController | null>(null);

  const listKey = `${endpoint}:${before ?? ""}:${reload}:${latestJob ?? ""}`;
  const page = versions?.key === listKey ? versions.data : undefined;
  const listError = versions?.key === listKey ? versions.error : undefined;
  const selectedId = page?.items.some(item => item.id === selection) ? selection : (page?.items[0]?.id ?? "");
  const detailKey = `${listKey}:${selectedId}`;
  const selected = details?.key === detailKey ? details.data : undefined;
  const detailError = details?.key === detailKey ? details.error : undefined;
  const downloading = downloadState?.key === detailKey ? downloadState.path : undefined;
  const downloadError = downloadState?.key === detailKey ? downloadState.error : undefined;

  useEffect(() => {
    const controller = new AbortController();
    const query = before ? `?before=${encodeURIComponent(before)}` : "";
    void api.get<VersionPage>(endpoint + query, undefined, controller.signal).then(next => {
      if (!controller.signal.aborted) setVersions({ key: listKey, data: next });
    }).catch(error => {
      if (!controller.signal.aborted) setVersions({ key: listKey, error: `辅助库版本暂时无法读取。${String(error)}` });
    });
    return () => controller.abort();
  }, [endpoint, before, listKey]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    void api.get<Publication>(`${endpoint}/${selectedId}`, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) setDetails({ key: detailKey, data: result });
    }).catch(error => {
      if (!controller.signal.aborted) setDetails({ key: detailKey, error: `所选版本暂时无法读取。${String(error)}` });
    });
    return () => {
      controller.abort();
      download.current?.abort();
    };
  }, [endpoint, selectedId, detailKey]);

  async function downloadFile(file: PublishedFile, id: string) {
    const controller = new AbortController();
    download.current?.abort();
    download.current = controller;
    setDownloadState({ key: detailKey, path: file.path });
    try {
      const token = localStorage.getItem("vf_token");
      const query = new URLSearchParams({ path: file.path });
      const response = await fetch(`/api/v1${endpoint}/${id}/artifact?${query}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`原件读取失败（${response.status}）`);
      const bytes = await response.blob();
      if (bytes.size !== file.size) throw new Error("原件大小与所选版本不符");
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(bytes);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.path.split("/").pop() ?? "flotherm-artifact";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setDownloadState(null);
    } catch (error) {
      if (!controller.signal.aborted) setDownloadState({ key: detailKey, error: `辅助库文件下载失败。${String(error)}` });
    } finally {
      if (download.current === controller) {
        download.current = null;
        if (controller.signal.aborted) setDownloadState(state => state?.key === detailKey ? null : state);
      }
    }
  }

  return <section className="flotherm-library-history" aria-label="已发布的辅助库">
    <div className="flotherm-library-heading">
      <h3>已发布版本</h3>
      <button type="button" onClick={() => setReload(value => value + 1)}>刷新版本</button>
    </div>
    {listError && <p className="flotherm-error" role="alert">{listError}</p>}
    {!page && !listError && <p role="status">正在读取辅助库版本…</p>}
    {page?.items.length === 0 && <p>尚未发布辅助库。完成一次编译后，库和日志会出现在这里。</p>}
    {page && page.items.length > 0 && <>
      <div className="flotherm-field">
        <label htmlFor={picker}>发布版本</label>
        <select id={picker} value={selectedId} onChange={event => setSelection(event.target.value)}>
          {page.items.map(item => <option key={item.id} value={item.id}>
            {item.current ? "当前发布" : "历史发布"} · 第 {item.generation} 版 · 作业 #{item.job_id} · {publishDate.format(new Date(item.published_at))}
          </option>)}
        </select>
      </div>
      {detailError && <p className="flotherm-error" role="alert">{detailError}</p>}
      {selectedId && !selected && !detailError && <p role="status">正在读取所选版本…</p>}
      {selected && <div className="flotherm-library-selection">
        <p>发布目录 <code>{selected.destination_dir}</code> · OpenFOAM {selected.openfoam}</p>
        <button type="button" onClick={() => {
          useStore.getState().requestJobPanel({ caseId, jobId: selected.job_id });
          activatePanel("jobs");
        }}>打开编译作业</button>
        <ul className="flotherm-file-list flotherm-library-files">
          {selected.files.map(file => <li key={file.path}>
            <div>
              <strong>{file.path.split("/").pop()}</strong>
              {file.role === "library" && <code>SHA-256 {file.sha256}</code>}
            </div>
            <button type="button" disabled={!!downloading}
              aria-label={`${fileLabels[file.role]} ${file.path.split("/").pop()}`}
              onClick={() => void downloadFile(file, selected.id)}>
              {downloading === file.path ? "正在下载…" : fileLabels[file.role]}
            </button>
          </li>)}
        </ul>
        {downloadError && <p className="flotherm-error" role="alert">{downloadError}</p>}
      </div>}
    </>}
    {(before || page?.next) && <div className="flotherm-actions">
      {before && <button type="button" onClick={() => setBefore(null)}>返回最新版本</button>}
      {page?.next && <button type="button" onClick={() => setBefore(page.next)}>更早版本</button>}
    </div>}
  </section>;
}
