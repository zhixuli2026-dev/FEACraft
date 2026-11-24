import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';
import { useStore } from '../store';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import { chunkHash, identifyFile, UPLOAD_CHUNK, UploadPaused } from '../utils/uploadResume';
import type { FileVersion, SourceFile, UploadStatus } from '../utils/uploadResume';
import './UploadRecovery.css';

type Entry = { id:string; path:string|null; size:number|null; offset:number|null; status:string; message?:string; manual_source?:string };
type Block = { size:number; sha256:string };
type Plan = { id:string; path:string; size:number; offset:number; snapshot_sha256:string; prefix_blocks:Block[];
  target:FileVersion; target_matches:boolean; original_file_identity_known:boolean; available_actions:string[];
  blocked:{ code:string; message:string }|null };
type Body = { action:'retain_for_review'|'resume_upload'|'cancel_upload'; upload_id:string; snapshot_sha256:string;
  note:string; source_blocks:string[]|null; prefix_blocks:Block[]|null };
type Pending = { schema:1; userId:number; caseId:number; key:string; body:Body; target:FileVersion; size:number; sourceName:string };
type Receipt = { id:string; upload_id:string; action:string; note:string; state:string; recorded_at:string;
  result:{ upload?:UploadStatus }|null };
type Page<T> = { entries:T[]; has_more:boolean; next_cursor:string|number|null };
export type AdoptedUpload = { key:string; upload:UploadStatus; version:FileVersion; source:SourceFile; file:File|null };
const names:Record<string,string> = { retain_for_review:'登记保留上传', resume_upload:'接续上传', cancel_upload:'取消并清理上传' };
const bytes = (value:number) => value < 1024 ? `${value} B` : value < 1024*1024 ? `${(value/1024).toFixed(1)} KiB` : `${(value/1024/1024).toFixed(1)} MiB`;
const hash = /^[0-9a-f]{64}$/;

function message(error:unknown) {
  if (error instanceof ApiError) {
    if (error.status===401) return '登录已失效。重新登录后可继续核对同一次确认。';
    if (error.status===403 || error.status===404) return '当前账户无法读取该上传，或原位置已变化。请刷新列表并查看历史记录。';
    if (error.code==='input_stale') return '上传来源或目标已经变化。本次确认未继续，请取消被拒绝的确认并重新核对。';
    return typeof (error.detail as { message?:unknown })?.message==='string'
      ? (error.detail as { message:string }).message : error.message;
  }
  return error instanceof TypeError ? '连接中断，结果尚待核对。请保留同一次确认，稍后继续。'
    : error instanceof Error ? error.message : '暂时无法核对上传，请保留原文件后重试。';
}

export default function UploadRecovery({caseId,userId,busy,occupied,onResume,onReviewUpload}:{caseId:number;userId:number;busy:boolean;occupied:boolean;onResume:(value:AdoptedUpload)=>Promise<void>;onReviewUpload?:(identifier:string)=>void}) {
  const [open,setOpen]=useState(false),[loading,setLoading]=useState(false),[acting,setActing]=useState(false);
  const [entries,setEntries]=useState<Entry[]|null>(null),[cursor,setCursor]=useState<string|null>(null);
  const [history,setHistory]=useState<Receipt[]>([]),[historyCursor,setHistoryCursor]=useState<number|null>(null);
  const [plan,setPlan]=useState<Plan|null>(null),[note,setNote]=useState(''),[choice,setChoice]=useState<{source:SourceFile;prefix:Block[];file:File}|null>(null);
  const [pending,setPending]=useState<Pending|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [corrupt,setCorrupt]=useState(false),[revise,setRevise]=useState(false),[hashed,setHashed]=useState(0);
  const live=useRef(true),running=useRef(false),reads=useRef<AbortController|null>(null),request=useRef<AbortController|null>(null);
  const key=`vf-upload-recovery-v1:${userId}:${caseId}`,base=`/cases/${caseId}/upload-recovery`;
  const current=()=>live.current && useStore.getState().user?.id===userId && useStore.getState().currentCase?.id===caseId;
  useEffect(()=>{
    live.current=true;
    try {
      const raw=sessionStorage.getItem(key);
      if (raw) {
        const value=JSON.parse(raw) as Pending;
        if (raw.length>65536 || value.schema!==1 || value.userId!==userId || value.caseId!==caseId
          || !/^[0-9a-f-]{36}$/.test(value.key) || !names[value.body?.action] || !/^[0-9a-f]{32}$/.test(value.body.upload_id)
          || !hash.test(value.body.snapshot_sha256) || !value.body.note?.trim() || !value.target || value.target.protected!==false
          || !Number.isSafeInteger(value.size) || value.size<1 || value.size>1024**3
          || value.body.action==='resume_upload' && (!Array.isArray(value.body.source_blocks)
            || value.body.source_blocks.length!==Math.ceil(value.size/UPLOAD_CHUNK) || value.body.source_blocks.some(v=>!hash.test(v)))) throw new Error();
        setPending(value);
      }
    } catch {setCorrupt(true);setError('本机确认记录已损坏。请先查看服务端历史；清除本机记录不会修改服务器上的上传。');}
    return ()=>{live.current=false;reads.current?.abort();request.current?.abort();};
  },[key,userId,caseId]);
  const disabled=busy||loading||acting||corrupt;

  async function refresh() {
    reads.current?.abort();const controller=new AbortController();reads.current=controller;setLoading(true);setError('');
    try {
      const [list,records]=await Promise.all([jobRecoveryRequest<Page<Entry>>(base,{signal:controller.signal}),jobRecoveryRequest<Page<Receipt>>(`${base}/actions`,{signal:controller.signal})]);
      if(current()&&!controller.signal.aborted){setEntries(list.entries);setCursor(list.has_more?list.next_cursor as string:null);
        setHistory(records.entries);setHistoryCursor(records.has_more?records.next_cursor as number:null);}
    } catch(failure){if(current()&&!controller.signal.aborted)setError(message(failure));}
    finally{if(current()&&!controller.signal.aborted)setLoading(false);}
  }
  useEffect(()=>{if(open)void refresh();},[open]);
  async function more(records=false) {
    const after=records?historyCursor:cursor;if(after===null)return;setLoading(true);setError('');
    try {
      const page=await jobRecoveryRequest<Page<Entry|Receipt>>(`${base}${records?'/actions':''}?cursor=${after}`);
      if(!current())return;
      if(records){setHistory(previous=>[...previous,...(page.entries as Receipt[])]);setHistoryCursor(page.has_more?page.next_cursor as number:null);}
      else{setEntries(previous=>[...(previous??[]),...(page.entries as Entry[])]);setCursor(page.has_more?page.next_cursor as string:null);}
    }catch(failure){if(current())setError(message(failure));}finally{if(current())setLoading(false);}
  }
  async function inspect(identifier:string) {
    setLoading(true);setPlan(null);setChoice(null);setError('');setNotice('');setHashed(0);
    try{const value=await jobRecoveryRequest<Plan>(`${base}/${identifier}`);if(current())setPlan(value);}
    catch(failure){if(current())setError(message(failure));}finally{if(current())setLoading(false);}
  }
  async function choose(file:File) {
    if(!plan)return;const selected=plan;setLoading(true);setChoice(null);setError('');setHashed(0);
    try {
      if(file.size!==selected.size)throw new Error('所选文件大小与原上传不同，请重新选择原文件。');
      const identified=await identifyFile(file,()=>!current(),value=>{if(current())setHashed(value);},undefined,true);
      const prefix:Block[]=[];
      for(let i=0;i<selected.prefix_blocks.length;i++){
        const block=selected.prefix_blocks[i],value=await chunkHash(file.slice(i*UPLOAD_CHUNK,i*UPLOAD_CHUNK+block.size));
        if(value!==block.sha256)throw new Error('所选文件与已上传部分不同，未发送续传确认。原上传继续保留。');
        prefix.push({size:block.size,sha256:value});
      }
      if(current()){setChoice({source:identified,prefix,file});setNotice('所选文件与已上传部分一致，可以明确确认接续。');}
    }catch(failure){if(current()&&!(failure instanceof UploadPaused))setError(message(failure));}finally{if(current())setLoading(false);}
  }
  async function submit(action?:Body['action']) {
    if(running.current||busy||!current())return;
    let confirmation=pending;
    if(!confirmation){
      if(!plan||!action||occupied||!plan.available_actions.includes(action)||!note.trim())return;
      const selected=action==='resume_upload';
      if(selected&&!choice&&plan.offset!==plan.size)return;
      const body:Body={action,upload_id:plan.id,snapshot_sha256:plan.snapshot_sha256,note:note.trim(),
        source_blocks:selected?(choice?.source.blocks??plan.prefix_blocks.map(v=>v.sha256)):null,
        prefix_blocks:selected?(choice?.prefix??plan.prefix_blocks):null};
      confirmation={schema:1,userId,caseId,key:crypto.randomUUID(),body,target:plan.target,size:plan.size,sourceName:choice?.source.name??plan.path.split('/').pop()!};
      try{const raw=JSON.stringify(confirmation);if(raw.length>65536)throw new Error();sessionStorage.setItem(key,raw);}
      catch{setError('无法保存本次确认，尚未发送。请允许浏览器会话存储后重试。');return;}
      setPending(confirmation);
    }
    running.current=true;setActing(true);setError('');setNotice('');setRevise(false);
    const controller=new AbortController();request.current=controller;
    try{
      const result=await jobRecoveryRequest<Receipt>(`${base}/actions`,{body:confirmation.body,key:confirmation.key,signal:controller.signal});
      if(!current())return;
      if(result.state!=='completed'){setNotice('已找到原确认，结果仍待核对。请稍后继续同一次确认。');return;}
      if(confirmation.body.action==='resume_upload'){
        if(!result.result?.upload)throw new Error('服务端未提供可核验的上传记录，请保留同一次确认。');
        await onResume({key:confirmation.key,upload:result.result.upload,version:confirmation.target,
          source:{name:confirmation.sourceName,size:confirmation.size,blocks:confirmation.body.source_blocks!},file:choice?.file??null});
      }
      if(!current())return;
      sessionStorage.removeItem(key);setPending(null);setPlan(null);setChoice(null);setNotice(`${names[confirmation.body.action]}已完成，操作记录保留。`);await refresh();
    }catch(failure){if(current()&&!controller.signal.aborted){setError(message(failure));
      if(failure instanceof ApiError&&([400,422].includes(failure.status)||['input_stale','file_conflict'].includes(failure.code??'')))setRevise(true);}}
    finally{running.current=false;if(current())setActing(false);}
  }
  function clearPending(){try{sessionStorage.removeItem(key);setPending(null);setCorrupt(false);setRevise(false);setError('');setPlan(null);void refresh();}catch{setError('无法清除本机确认，请保留记录后重试。');}}

  return <details className="upload-recovery" data-testid="upload-recovery" onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>服务端未完成上传</summary>
    {open&&<section aria-label="服务端未完成上传" aria-busy={loading||acting}>
      <p>本机记录遗失后，可核对服务端保留的上传。接续需要重新选择原文件；取消会清理暂存内容，保留目标文件和操作记录。</p>
      <button type="button" disabled={busy||loading||acting} onClick={()=>void refresh()}>刷新上传与操作记录</button>
      {loading&&<p role="status">正在核对{hashed?` · ${bytes(hashed)}`:'…'}</p>}
      {corrupt&&<button type="button" disabled={busy||acting} onClick={clearPending}>清除损坏的本机确认</button>}
      {pending?<div className="upload-recovery-confirm">
        <p>有一次“{names[pending.body.action]}”尚待核对。继续会沿用原标识与原确认内容。</p>
        <button type="button" disabled={disabled} onClick={()=>void submit()}>继续同一次上传确认</button>
        {revise&&<button type="button" disabled={busy||acting} onClick={clearPending}>取消被拒绝的本机确认</button>}
      </div>:<>
        {occupied&&<p>当前已有文件操作。请先处理该操作，再确认服务端上传。</p>}
        {entries!==null&&entries.length===0&&!loading&&<p>本页未发现可处理的上传。已有文件和操作记录未修改。</p>}
        <ul>{entries?.map(row=><li key={row.id}>
          <p><strong>{row.path??'无法核验的上传'}</strong><br/>{row.offset===null?'进度未知':`${bytes(row.offset)} / ${bytes(row.size!)}`}</p>
          {row.message&&<p>{row.message}</p>}
          {row.manual_source&&onReviewUpload
            ? <button type="button" disabled={disabled} onClick={()=>onReviewUpload(row.id)}>核查原始上传现场</button>
            : <button type="button" disabled={disabled} onClick={()=>void inspect(row.id)}>核对此上传</button>}
        </li>)}</ul>
        {cursor!==null&&<button type="button" disabled={disabled} onClick={()=>void more()}>查看下一页上传</button>}
        {plan&&<div className="upload-recovery-selected">
          <h4>核对上传与原文件</h4><p>{plan.path}<br/>已保留 {bytes(plan.offset)} / {bytes(plan.size)}</p>
          {!plan.target_matches&&<p>目标文件已变化，不能接续此上传；可登记保留或明确取消。</p>}
          {!plan.original_file_identity_known&&<p>原上传未登记整文件摘要。请确认所选原文件；已上传部分会逐块核对。</p>}
          {plan.blocked&&<p>{plan.blocked.message}</p>}
          {plan.available_actions.includes('resume_upload')&&plan.offset<plan.size&&<label>重新选择原文件
            <input type="file" data-testid="upload-recovery-original" disabled={disabled||occupied} onChange={event=>{const file=event.currentTarget.files?.[0];event.currentTarget.value='';if(file)void choose(file);}}/>
          </label>}
          {choice&&<p>已核对：{choice.source.name} · {bytes(choice.source.size)}</p>}
          {plan.offset===plan.size&&<p>所有内容已收齐。确认接续后，可在文件操作中明确完成上传。</p>}
          <label>处理原因<textarea value={note} rows={3} maxLength={2000} disabled={disabled} onChange={event=>setNote(event.target.value)}/></label>
          <div className="upload-recovery-buttons">
            <button type="button" disabled={disabled||occupied||!note.trim()||!plan.available_actions.includes('retain_for_review')} onClick={()=>void submit('retain_for_review')}>登记保留上传</button>
            <button type="button" disabled={disabled||occupied||!note.trim()||!plan.available_actions.includes('resume_upload')||!choice&&plan.offset!==plan.size} onClick={()=>void submit('resume_upload')}>确认接续此上传</button>
            <button type="button" disabled={disabled||occupied||!note.trim()||!plan.available_actions.includes('cancel_upload')} onClick={()=>void submit('cancel_upload')}>确认取消并清理上传</button>
          </div>
        </div>}
      </>}
      {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <h4>上传人工操作记录</h4>
      {!history.length&&!loading&&<p>尚无人工操作记录。</p>}
      <ul>{history.map(row=><li key={row.id}><p><strong>{names[row.action]??'人工处理'}</strong> · {row.state==='completed'?'已完成':'待核对'}<br/>{row.note}</p><p className="upload-recovery-time">{row.recorded_at}</p></li>)}</ul>
      {historyCursor!==null&&<button type="button" disabled={disabled} onClick={()=>void more(true)}>查看下一页操作记录</button>}
    </section>}
  </details>;
}
