import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useStore } from "../store";
import { UPLOAD_CHUNK as CHUNK, UPLOAD_INLINE as INLINE, UPLOAD_MAX as MAX,
  InvalidResumeRecord, UploadPaused, chunkHash, identifyFile, readResume, saveResume,
  removeResume, removeInvalidResume, resumeScope, validateUpload } from "../utils/uploadResume";
import type { FileVersion as Version, ResumeRecord, UploadStatus } from "../utils/uploadResume";
import UploadRecovery from './UploadRecovery';
import type { AdoptedUpload } from './UploadRecovery';
import "./CaseFileActions.css";

type Operation = {kind:"upload"; path:string; file:File|null} | {kind:"delete"; path:string};
type Phase = "" | "checking" | "hashing" | "uploading" | "publishing" | "deleting" | "cancelling" | "reconciling";
type LegacyUpload = {id:string;path:string;size:number;remaining_bytes:number;storage_binding_valid:boolean;
  owner_reconciliation_required:boolean;can_reconcile_owner:boolean};
const bytes = (size:number) => size < 1024 ? `${size} B` : size < 1024*1024
  ? `${(size/1024).toFixed(1)} KiB` : `${(size/1024/1024).toFixed(1)} MiB`;

/** Remount on account changes too; async work keeps its original user/case scope. */
export default function CaseFileActions(props:{caseId:number; caseName:string; onReviewUpload?:(identifier:string)=>void}) {
  const userId=useStore(state=>state.user?.id);
  return userId ? <FileActions key={`${userId}:${props.caseId}`} {...props} userId={userId}/> : null;
}
function FileActions({caseId,caseName,userId,onReviewUpload}:{caseId:number;caseName:string;userId:number;onReviewUpload?:(identifier:string)=>void}) {
  const [operation,setOperation] = useState<Operation|null>(null);
  const [version,setVersion] = useState<Version|null>(null);
  const [record,setRecord] = useState<ResumeRecord|null>(null);
  const [phase,setPhase] = useState<Phase>("checking");
  const [error,setError] = useState("");
  const [notice,setNotice] = useState("");
  const [stopping,setStopping] = useState(false);
  const [hashed,setHashed] = useState(0);
  const [invalid,setInvalid] = useState(false);
  const [legacy,setLegacy] = useState<LegacyUpload[]|null>(null);
  const [legacyLoading,setLegacyLoading] = useState(false);
  const [legacyPending,setLegacyPending] = useState("");
  const [legacyError,setLegacyError] = useState("");
  const [legacyNotice,setLegacyNotice] = useState("");
  const phaseRef = useRef<Phase>("checking");
  const recordRef = useRef<ResumeRecord|null>(null);
  const alive = useRef(true), stop = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null), resumeInput = useRef<HTMLInputElement>(null);
  const current = () => alive.current && useStore.getState().currentCase?.id===caseId && useStore.getState().user?.id===userId;
  const stopped = () => stop.current || !current();
  const prefix=`/cases/${caseId}`,scope=resumeScope(userId,caseId),busy=phase!=="",session=record?.upload;
  const changePhase = (value:Phase) => {phaseRef.current=value;if(current()) setPhase(value);};
  const remember = async (value:ResumeRecord,create=false) => {
    await saveResume(value,create);recordRef.current=value;if(current()) setRecord(value);
  };
  const refresh = async () => {
    if (!current()) return;
    try {await useStore.getState().loadTree();}
    catch {if(current()) setNotice(text=>text+" 文件树刷新失败，可稍后刷新。");}
  };
  const complete = async (value:ResumeRecord) => {
    await removeResume(value);recordRef.current=null;
    if(current()) {setRecord(null);setOperation(null);setVersion(null);setNotice(`已上传 ${value.path}`);}
    await refresh();
  };
  const status = async (value:ResumeRecord) => {
    let upload:UploadStatus;
    try {
      if (!value.upload) {
        if (!value.keyedCreation) return value;
        const intent=await api.get<{upload:UploadStatus|null;cancelled?:boolean}>(`${prefix}/upload-intent`,value.clientId);
        if (intent.cancelled) {
          await removeResume(value);recordRef.current=null;
          if(current()){setRecord(null);setOperation(null);setVersion(null);setNotice("这次上传已取消。");}
          return null;
        }
        if (!intent.upload) return value;
        upload=validateUpload(intent.upload,value);
      } else upload=validateUpload(await api.get<UploadStatus>(`${prefix}/uploads/${value.upload.id}`),value);
    }
    catch(cause) {
      if(cause instanceof ApiError && cause.status===404)
        throw new Error("服务端已找不到这次上传，结果尚未确认。请先查看目标文件；取消本次上传后可重新检查，系统不会自动覆盖。");
      throw cause;
    }
    if (upload.status==="completed") {await complete(value);return null;}
    const next={...value,upload};await remember(next);return next;
  };
  const failure = (cause:unknown) => {
    if (!current()) return;
    if(cause instanceof UploadPaused) {setNotice("已暂停。重新选择或继续上传即可恢复。");return;}
    if(cause instanceof InvalidResumeRecord) setInvalid(true);
    if(cause instanceof ApiError && ["file_conflict","revision_conflict"].includes(cause.code??"")) {
      setVersion(null);
      setError(recordRef.current ? "目标文件已变化，本次未替换。请取消本次上传，再检查目标文件。"
        : operation?.kind==="delete" ? "目标文件已变化，本次未删除。请重新检查目标后决定。"
        : "目标文件已变化，本次未替换。所选内容已保留，请重新检查目标后决定。");
    } else setError(cause instanceof TypeError ? "网络请求中断，结果尚未确认。请查询上传结果或继续上传。"
      : cause instanceof Error ? cause.message : String(cause));
  };
  useEffect(()=>{
    alive.current=true;let disposed=false;
    void (async()=>{
      try {
        const saved=await readResume(scope);
        if(disposed || !current() || !saved) return;
        recordRef.current=saved;setRecord(saved);setVersion(saved.version);
        setOperation({kind:"upload",path:saved.path,file:null});
        const latest=await status(saved);
        if(latest && !disposed && current()) setNotice(!latest.upload && !latest.keyedCreation
          ? "旧版记录缺少服务端创建标识。请先结束本机记录，再在上传恢复中核对并接续原会话。"
          : latest.upload?.offset===latest.source.size
          ? "所有分块已上传，可直接完成上传或查询结果。"
          : "发现未完成的上传。重新选择原文件后可继续，已上传的部分会保留。");
      } catch(cause) {if(!disposed) failure(cause);}
      finally {if(!disposed) changePhase("");}
    })();
    return ()=>{disposed=true;alive.current=false;stop.current=true;};
  },[]);
  const inspect = async () => {
    if(!operation || phaseRef.current || recordRef.current) return;
    changePhase("checking");setError("");setNotice("");setVersion(null);
    try {
      const result=await api.get<Version>(`${prefix}/file:version?path=${encodeURIComponent(operation.path.trim())}`);
      if(!current()) return;
      if(result.exists && !result.content_sha256) throw new Error("无法确认文件版本，请重新检查。");
      setVersion(result);
      if(result.protected) setError("caseDict 需要通过编辑器保存，不能在这里上传或删除。");
    } catch(cause) {failure(cause);} finally {changePhase("");}
  };
  const query = async () => {
    if(phaseRef.current || !recordRef.current) return;
    changePhase("checking");setError("");
    try {const latest=await status(recordRef.current);if(latest && current()) setNotice("上传尚未完成，已更新进度。");}
    catch(cause) {failure(cause);} finally {changePhase("");}
  };
  const cancel = async () => {
    if(phaseRef.current) return;
    changePhase("cancelling");setError("");
    try {
      const value=recordRef.current;
      if(value && (value.upload || value.keyedCreation)) {
        try {
          if(value.upload) await api.del(`${prefix}/uploads/${value.upload.id}`);
          else await api.del(`${prefix}/upload-intent`,value.clientId);
        }
        catch(cause) {
          if(cause instanceof ApiError && cause.status===409 && await status(value)===null) return;
          if(!(value.upload && cause instanceof ApiError && cause.status===404)) throw cause;
        }
      }
      if(value) await removeResume(value);
      recordRef.current=null;
      if(current()) {setRecord(null);setOperation(null);setVersion(null);setNotice(value && !value.upload && !value.keyedCreation
        ? "已清除本机旧记录。请在上传恢复中核对服务端会话。" : value?"已结束本次上传；可重新检查目标文件。":"");}
    } catch(cause) {failure(cause);} finally {changePhase("");}
  };
  const chooseOriginal = async (file:File) => {
    const value=recordRef.current;
    if(!value || phaseRef.current) return;
    changePhase("hashing");stop.current=false;setStopping(false);setHashed(0);setError("");
    setOperation({kind:"upload",path:value.path,file:null});
    try {
      await identifyFile(file,stopped,n=>{if(current()) setHashed(n);},value.source,value.serverRecovered);
      if(current()) {setOperation({kind:"upload",path:value.path,file});setNotice("文件内容校验通过，可以继续上传。");}
    } catch(cause) {failure(cause);} finally {changePhase("");if(current()) setStopping(false);}
  };
  const submit = async () => {
    if(!operation || !version || version.protected || phaseRef.current || !current()) return;
    changePhase(operation.kind==="upload"?"uploading":"deleting");
    setError("");setNotice("");stop.current=false;setStopping(false);
    try {
      const path=version.path;
      if(operation.kind==="delete") {
        if(!version.exists || !version.content_sha256) throw new Error("目标文件不存在，无需删除。");
        await api.del(`${prefix}/file?path=${encodeURIComponent(path)}&expected_sha256=${version.content_sha256}`);
      } else if(operation.file && operation.file.size<=INLINE && !recordRef.current) {
        const data=new FormData();data.append("file",operation.file);
        const condition=version.exists?`expected_sha256=${version.content_sha256}`:"expected_absent=true";
        await api.upload(`${prefix}/upload?path=${encodeURIComponent(path)}&${condition}`,data);
      } else {
        if(operation.file && operation.file.size>MAX) throw new Error("单文件最大支持 1 GiB，请拆分后上传。");
        let value=recordRef.current;
        if(!value) {
          if(!operation.file) throw new Error("请重新选择原文件。");
          changePhase("hashing");setHashed(0);
          const source=await identifyFile(operation.file,stopped,n=>{if(current()) setHashed(n);});
          value={schema:1,scope,clientId:crypto.randomUUID(),path,version,source,upload:null,keyedCreation:true};
          await remember(value,true);
        }
        if(stopped()) throw new UploadPaused();
        changePhase("uploading");
        if(value.upload) {value=await status(value);if(!value) return;}
        else {
          if(!value.keyedCreation) throw new Error("请先结束本机旧记录，再在上传恢复中核对并接续原会话。");
          const upload=validateUpload(await api.post<UploadStatus>(`${prefix}/uploads`,{path:value.path,size:value.source.size,
            overwrite:value.version.exists,expected_sha256:value.version.content_sha256},value.clientId),value);
          if(upload.status==="completed"){await complete(value);return;}
          value={...value,upload};await remember(value);
        }
        let upload=value.upload!;
        while(upload.offset<value.source.size && !stopped()) {
          if(!operation.file) throw new Error("请重新选择原文件，校验通过后可继续上传。");
          const blockStart=Math.floor(upload.offset/CHUNK)*CHUNK;
          const chunkEnd=Math.min(blockStart+CHUNK,value.source.size);
          const fullBlock=operation.file.slice(blockStart,chunkEnd);
          if(await chunkHash(fullBlock)!==value.source.blocks[Math.floor(upload.offset/CHUNK)]) throw new Error("文件内容已变化，请重新选择原文件。已上传的部分仍保留。");
          const chunk=operation.file.slice(upload.offset,chunkEnd);
          if(stopped()) break;
          const previousOffset=upload.offset;
          upload=validateUpload(await api.putBytes<UploadStatus>(`${prefix}/uploads/${upload.id}?offset=${upload.offset}`,chunk),value);
          if(upload.offset!==previousOffset+chunk.size) throw new Error("服务端返回了意外的上传进度，请查询上传结果后再继续。");
          value={...value,upload};await remember(value);
        }
        if(stopped()) throw new UploadPaused();
        changePhase("publishing");await api.post(`${prefix}/uploads/${upload.id}:complete`);
        await complete(value);return;
      }
      if(current()) {
        setNotice(operation.kind==="upload"?`已上传 ${path}`:`已删除 ${path}；已打开的编辑内容仍保留。`);
        setOperation(null);setVersion(null);
      }
      await refresh();
    } catch(cause) {failure(cause);} finally {changePhase("");if(current()) setStopping(false);}
  };
  const begin = (value:Operation) => {
    if(phaseRef.current || operation || invalid) return;
    setOperation(value);setVersion(null);setError("");setNotice("");
  };
  const clearInvalid = async () => {
    changePhase("cancelling");
    try {await removeInvalidResume(scope);if(current()) {setInvalid(false);setError("");setNotice("已清除本机损坏记录，服务端文件未修改。");}}
    catch(cause) {failure(cause);} finally {changePhase("");}
  };
  const adopt = async (value:AdoptedUpload) => {
    if(!current()||phaseRef.current)throw new Error('当前文件操作尚未结束，请保留同一次上传确认后重试。');
    const previous=recordRef.current;
    if(previous&&previous.clientId!==value.key)throw new Error('本机已有另一条上传记录，请先处理该上传。');
    const next:ResumeRecord={schema:1,scope,clientId:value.key,path:value.version.path,version:value.version,
      source:value.source,upload:value.upload,serverRecovered:true};
    validateUpload(value.upload,next);await remember(next,!previous);
    if(current()){setVersion(next.version);setOperation({kind:'upload',path:next.path,file:value.file});setError('');
      setNotice('已接续服务端上传。核对后点击继续上传或完成上传。');}
  };
  const inspectLegacy = async () => {
    if(!current()) return;
    setLegacyLoading(true);setLegacyError("");
    try {
      const rows=await api.get<LegacyUpload[]>(`${prefix}/uploads`);
      if(current()) setLegacy(rows.filter(row=>row.owner_reconciliation_required));
    } catch(cause) {
      if(current()) setLegacyError(cause instanceof Error?cause.message:"无法检查旧上传，请重试。");
    } finally {if(current()) setLegacyLoading(false);}
  };
  const reconcileOwner = async (upload:LegacyUpload) => {
    if(phaseRef.current || !current() || !upload.can_reconcile_owner || !upload.storage_binding_valid) return;
    changePhase("reconciling");setLegacyPending(upload.id);setLegacyError("");setLegacyNotice("");
    try {
      await api.post(`${prefix}/uploads/${upload.id}:reconcile-owner`);
      if(!current()) return;
      setLegacyNotice(`已修复 ${upload.path} 的额度归属。原上传可以继续，已上传内容保留。`);
      await inspectLegacy();
    } catch(cause) {
      if(current()) setLegacyError(cause instanceof TypeError
        ?"网络请求中断，修复结果尚未确认。检查旧上传后可重试，系统不会重复转移额度。"
        :cause instanceof Error?cause.message:"归属修复未完成，请检查后重试。");
    } finally {changePhase("");if(current()) setLegacyPending("");}
  };
  return <section className="vf-file-actions" data-testid="case-file-actions" style={{marginTop:12}}>
    <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap",marginBottom:6}}>
      <span className="vf-file-actions-title" title={caseName} style={{flex:"1 1 180px",minWidth:0,color:"var(--vf-muted)"}}>文件 · {caseName}</span>
      <div className="vf-file-actions-buttons">
      <button style={s.button} disabled={busy||!!operation||invalid} onClick={()=>fileInput.current?.click()}>上传</button>
      <input ref={fileInput} data-testid="tree-upload-file" aria-label="选择要上传的文件" type="file" disabled={busy||!!operation||invalid}
        style={{display:"none"}} onChange={event=>{
          const file=event.currentTarget.files?.[0];event.currentTarget.value="";
          if(file) begin({kind:"upload",file,path:`Ref/${file.name}`});
        }}/>
      <button data-testid="tree-delete-file" style={s.button} disabled={busy||!!operation||invalid}
        onClick={()=>begin({kind:"delete",path:useStore.getState().activeFile??""})}>删除</button>
      <button data-testid="file-legacy-inspect" style={s.button} disabled={busy||legacyLoading} onClick={inspectLegacy}>
        {legacyLoading?"正在检查…":"检查旧上传"}</button>
      </div>
    </div>
    {(legacy!==null || legacyError) && <div className="vf-upload-owner" data-testid="upload-owner-recovery" aria-busy={legacyLoading||!!legacyPending}>
      {legacy!==null && (legacy.length===0
        ? <p style={s.text}>未发现需要修复额度归属的旧上传。</p>
        : <>
          <p style={s.text}>这些旧上传的额度仍计在协作者名下。修复后，待上传额度计入项目所有者，已上传内容保留。</p>
          <ul className="vf-upload-owner-list">
            {legacy.map(upload=><li key={upload.id} data-testid="upload-owner-row">
              <p className="vf-upload-owner-path">{upload.path}</p>
              <p style={s.text}>账面剩余额度 {bytes(upload.remaining_bytes)}，修复时按服务端进度重新核算。</p>
              {!upload.storage_binding_valid
                ? <p className="vf-file-error" style={s.text}>存储位置已变化，请先核对上传记录。</p>
                : !upload.can_reconcile_owner
                  ? <p style={s.text}>请联系项目所有者修复额度归属后继续上传。</p>
                  : <button data-testid="upload-owner-reconcile" style={s.button} disabled={busy||legacyLoading} onClick={()=>reconcileOwner(upload)}>
                    {legacyPending===upload.id?"正在修复归属…":"由项目所有者核算"}</button>}
            </li>)}
          </ul>
        </>)}
      {legacyError && <p data-testid="upload-owner-error" role="alert" className="vf-file-error" style={s.text}>{legacyError}</p>}
      {legacyNotice && <p data-testid="upload-owner-notice" role="status" style={s.text}>{legacyNotice}</p>}
    </div>}
    <UploadRecovery caseId={caseId} userId={userId} busy={busy} occupied={!!operation||invalid} onResume={adopt} onReviewUpload={onReviewUpload}/>
    {operation && <div data-testid="file-operation-form" style={{paddingBlock:8,borderBlock:"1px solid var(--vf-border)",marginBottom:8}}>
      {operation.kind==="upload" && <p style={s.text}>待上传：{operation.file?.name??record?.source.name} · {bytes(operation.file?.size??record?.source.size??0)}</p>}
      <label style={{display:"grid",gap:4}}>算例内文件路径
        <input data-testid="file-operation-path" value={operation.path} disabled={busy||!!record}
          style={s.input} onChange={event=>{setOperation({...operation,path:event.target.value});setVersion(null);setError("");setNotice("");}}/>
      </label>
      {version && <p data-testid="file-target-version" style={s.text}>
        {version.exists?`服务器已有该文件 · ${bytes(version.size)}`:"服务器上尚无此文件"}
        {operation.kind==="delete" && version.exists?"。确认删除后无法在回收站恢复。":""}
      </p>}
      {record && operation.kind==="upload" && <div>
        {!operation.file && <p style={s.text}>{session?.offset===record.source.size?"所有分块已上传，可直接完成上传或查询结果。":"请重新选择原文件。只核对文件内容，不会重复上传已完成的部分。"}</p>}
        <button style={s.button} disabled={busy} onClick={()=>resumeInput.current?.click()}>重新选择原文件</button>
        <input ref={resumeInput} data-testid="file-resume-source" type="file" aria-label="重新选择原文件" disabled={busy} style={{display:"none"}}
          onChange={event=>{const file=event.currentTarget.files?.[0];event.currentTarget.value="";if(file) void chooseOriginal(file);}}/>
      </div>}
      {phase==="hashing" && <div style={{marginTop:8}}>
        <progress aria-label="文件校验进度" max={operation.kind==="upload"?record?.source.size??operation.file?.size:1} value={hashed}/>
        <div role="status">正在校验文件内容 · {bytes(hashed)}</div>
      </div>}
      {session && <div style={{marginTop:8}}>
        <progress aria-label="文件上传进度" max={session.size} value={session.offset}/>
        <div role="status">{bytes(session.offset)} / {bytes(session.size)}</div>
      </div>}
      <div style={{display:"flex",flexWrap:"wrap",gap:6,marginTop:8}}>
        {record ? <button data-testid="file-upload-status" style={s.button} disabled={busy||(!session&&!record.keyedCreation)} onClick={query}>查询上传结果</button>
          : <button data-testid="file-operation-inspect" style={s.button} disabled={busy||!operation.path.trim()} onClick={inspect}>
            {phase==="checking"?"检查中…":"检查目标文件"}</button>}
        <button data-testid="file-operation-submit" className={operation.kind==="delete"?"vf-file-danger":"vf-file-primary"} style={s.button}
          disabled={busy||!version||version.protected||operation.kind==="delete"&&!version.exists||
            operation.kind==="upload"&&!operation.file&&(!session||session.offset!==session.size)} onClick={submit}>
          {phase==="hashing"?"校验中…":phase==="uploading"?"上传中…":phase==="publishing"?"正在完成上传…":phase==="deleting"?"删除中…"
            :operation.kind==="delete"?"确认删除":session?.offset===session?.size&&!!session?"完成上传":record?"继续上传":version?.exists?"确认替换文件":"上传文件"}
        </button>
        {(phase==="hashing" || phase==="uploading") && operation.kind==="upload" && (record||operation.file&&operation.file.size>INLINE)
          ? <button data-testid="file-upload-pause" style={s.button} disabled={stopping} onClick={()=>{stop.current=true;setStopping(true);}}>
              {stopping?"正在暂停…":"暂停上传"}</button>
          : <button data-testid="file-operation-cancel" style={s.button} disabled={busy} onClick={cancel}>{record?"取消本次上传":"取消"}</button>}
      </div>
    </div>}
    {error && <p data-testid="file-operation-error" className="vf-file-error" role="alert" style={s.text}>{error}</p>}
    {invalid && <button style={s.button} disabled={busy} onClick={clearInvalid}>清除本机损坏记录</button>}
    {notice && <p data-testid="file-operation-notice" role="status" style={s.text}>{notice}</p>}
  </section>;
}
const s:Record<string,React.CSSProperties> = {
  button:{padding:"4px 8px",background:"var(--vf-panel)",color:"var(--vf-text)",border:"1px solid var(--vf-border)",borderRadius:5,cursor:"pointer",fontSize:12},
  input:{width:"100%",boxSizing:"border-box",minWidth:0,padding:"5px 8px",background:"var(--vf-bg)",color:"var(--vf-text)",border:"1px solid var(--vf-border)",borderRadius:5,fontSize:13},
  text:{margin:"6px 0",overflowWrap:"anywhere",fontSize:13,color:"var(--vf-text)"},
};
