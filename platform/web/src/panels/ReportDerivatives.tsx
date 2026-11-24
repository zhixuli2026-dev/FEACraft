import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { readDerivative, readDerivativeFile, saveDerivative } from "../utils/reportDerivative";
import type { Derivative, DerivativeFile } from "../utils/reportDerivative";
import { publicationLabel, usePublicationStatus } from '../utils/publicationStatus';
import { readDerivativeSelection, readDerivativeView, saveDerivativeSelection, saveDerivativeView, type DerivativeFamily } from '../utils/viewSelection';
import "./ReportHistory.css";

interface Row { id: string; family: string; output: string | null; generation: number; status: string; current: boolean }
const names: Record<string,string> = {draft:"报告草稿", "pdf-number":"页码 PDF", ppt:"PPTX"};

/** Case-keyed readonly archive; no selection writes to the editable workspace. */
export default function ReportDerivatives({caseId,userId}: {caseId:number;userId:number}) {
  const [initial]=useState(()=>readDerivativeView(userId,caseId));
  const [open,setOpen]=useState(initial.open),[family,setFamily]=useState<DerivativeFamily>(initial.family);
  return <section className="report-history report-derivatives" aria-label="报告产物历史" data-testid="report-derivatives">
    <button type="button" aria-expanded={open} onClick={()=>{setOpen(!open);saveDerivativeView(userId,caseId,!open,family);}}>草稿、页码 PDF 与 PPT 历史</button>
    {open && <>
      <p>查看已保存的版本；选择和下载不会替换工作区文件。</p>
      <label>产物类型 <select value={family} onChange={event=>{const next=event.target.value as DerivativeFamily;setFamily(next);saveDerivativeView(userId,caseId,open,next);}}>
        <option value="">全部类型</option>{Object.entries(names).map(([value,name])=><option key={value} value={value}>{name}</option>)}
      </select></label>
      <Archive key={`${userId}:${caseId}:${family}`} userId={userId} caseId={caseId} family={family} />
    </>}
  </section>;
}

function Archive({caseId,family,userId}: {caseId:number;family:DerivativeFamily;userId:number}) {
  const [rows,setRows]=useState<Row[]>([]),[busy,setBusy]=useState(false),[more,setMore]=useState(true),[error,setError]=useState("");
  const [selection,setSelection]=useState(()=>readDerivativeSelection(userId,caseId,family));
  const id=selection.id;
  const select=(next:string|null)=>{setSelection({id:next,invalid:false});saveDerivativeSelection(userId,caseId,family,next);};
  const alive=useRef(true),lock=useRef(false),retryReset=useRef(true);
  useEffect(()=>{alive.current=true;void load(true);return()=>{alive.current=false;};},[]);
  async function load(reset=false) {
    if(lock.current)return;lock.current=true;retryReset.current=reset;setBusy(true);setError("");
    try {
      const cursor=!reset && rows.length ? rows[rows.length-1].id : null;
      const values=await api.get<Row[]>(`/cases/${caseId}/report/derivatives?limit=20`
        +(family?`&family=${encodeURIComponent(family)}`:"")+(cursor?`&before_id=${encodeURIComponent(cursor)}`:""));
      if(!Array.isArray(values)||values.some(row=>typeof row.id!=="string" || !names[row.family]
          || !Number.isInteger(row.generation) || typeof row.current!=="boolean"))throw Error("历史列表格式无效");
      if(!alive.current)return;
      setRows(previous=>reset?values:[...previous,...values.filter(row=>!previous.some(old=>old.id===row.id))]);
      setMore(values.length===20);
    }catch(cause){if(alive.current)setError(cause instanceof Error?cause.message:String(cause));}
    finally{lock.current=false;if(alive.current)setBusy(false);}
  }
  return <>
    <div className="report-history-heading"><button type="button" disabled={busy} onClick={()=>void load(true)}>刷新产物列表</button></div>
    <div className="report-history-list report-history-rows" aria-label="产物版本列表">
      {rows.map(row=><button type="button" key={row.id} data-derivative-id={row.id} aria-pressed={row.id===id}
        disabled={!["committed","superseded"].includes(row.status)} onClick={()=>select(row.id)}>
        <span>{names[row.family]} · 版本 {row.generation}</span><span>{row.output || "输出路径未记录"}</span>
        <span>{row.status==='superseded'?'保留产物 · 未成为当前有效结果':row.status==='committed'?row.current?"最新发布 · 选择时核验":"历史发布":row.status==="failed"?"发布失败":"尚未发布"}</span>
      </button>)}
    </div>
    {busy && <p role="status">正在读取历史列表…</p>}
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={()=>void load(retryReset.current)}>重试列表</button></p>}
    {!busy && !error && !rows.length && <p>此类型暂无已记录的产物版本。</p>}
    {more && rows.length>0 && <button type="button" disabled={busy} onClick={()=>void load()}>加载更早产物</button>}
    {selection.invalid && <p role="alert">保存的产物版本选择无效，请明确选择版本。<button type="button" onClick={()=>select(null)}>清除旧选择</button></p>}
    {id && <Selected key={id} caseId={caseId} userId={userId} family={family} id={id} close={()=>select(null)} />}
  </>;
}

function Selected({caseId,id,userId,family,close}: {caseId:number;id:string;userId:number;family:DerivativeFamily;close:()=>void}) {
  const [document,setDocument]=useState<Derivative|null>(null),[error,setError]=useState(""),[notice,setNotice]=useState("");
  const [text,setText]=useState<string|null>(null),[pdf,setPdf]=useState<string|null>(null),[busy,setBusy]=useState(false);
  const [reload,setReload]=useState(0);
  const controller=useRef<AbortController|null>(null),requestLock=useRef(false);
  const publicationState=usePublicationStatus(userId,caseId,'derivative',document?.publication??null);
  useEffect(()=>{
    const abort=new AbortController();controller.current=abort;let blobUrl:string|null=null;
    setDocument(null);setText(null);setPdf(null);setError("");setNotice("");setBusy(true);
    void (async()=>{
      try {
        const value=await readDerivative(caseId,id,abort.signal);
        if(abort.signal.aborted)return;
        if(family && value.family!==family)throw new Error('发布不属于所选产物类型，请重新选择。');
        if(value.family!=="ppt") {
          const bytes=await readDerivativeFile(caseId,id,value.files[0],abort.signal);
          if(abort.signal.aborted)return;
          if(value.family==="draft")setText(new TextDecoder().decode(bytes));
          else {blobUrl=URL.createObjectURL(new Blob([bytes],{type:"application/pdf"}));setPdf(blobUrl);}
        }
        setDocument(value);
      }catch(cause){if(!abort.signal.aborted)setError(cause instanceof Error?cause.message:String(cause));}
      finally{if(!abort.signal.aborted)setBusy(false);}
    })();
    return()=>{abort.abort();if(blobUrl)URL.revokeObjectURL(blobUrl);};
  },[caseId,id,family,reload]);
  async function download(file:DerivativeFile) {
    const abort=controller.current;if(!abort || abort.signal.aborted || requestLock.current || publicationState.blocked)return;
    requestLock.current=true;setBusy(true);setNotice("");
    try {
      const bytes=await readDerivativeFile(caseId,id,file,abort.signal);
      if(!abort.signal.aborted){saveDerivative(bytes,file.path);setNotice("已开始下载所选版本原件。");}
    }catch(cause){if(!abort.signal.aborted)setNotice(`下载失败：${cause instanceof Error?cause.message:String(cause)}；可重试下载。`);}
    finally{requestLock.current=false;if(!abort.signal.aborted)setBusy(false);}
  }
  return <section className="report-derivative-selected" aria-label="所选产物版本">
    <div className="report-history-heading"><strong>所选版本</strong><button type="button" onClick={close}>关闭版本</button></div>
    {busy && <p role="status">正在读取并核验原件…</p>}
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={()=>setReload(value=>value+1)}>重新读取版本</button></p>}
    {publicationState.error && <p role={publicationState.blocked?'alert':'status'}>{publicationState.error}</p>}
    {publicationState.blocked && <button type="button" disabled={busy} onClick={()=>setReload(value=>value+1)}>重读所选版本</button>}
    {document && !publicationState.blocked && <>
      <p data-testid="derivative-state">{names[document.family]} · {publicationLabel(publicationState.publication??document.publication)}</p>
      <p>{document.output}</p>
      {document.files.map(file=><button key={file.path} type="button" disabled={busy} onClick={()=>void download(file)}>
        {file.role==="previous-draft"?"下载生成前的草稿备份":"下载所选版本原件"}</button>)}
      {text!==null && (text.trim()?<pre tabIndex={0} aria-label="只读历史草稿">{text}</pre>:<p role="status">所选草稿没有可显示的正文，可下载原件。</p>)}
      {pdf && <iframe title="所选历史页码 PDF" src={pdf} />}
      {document.family==="ppt" && <p>PPTX 原件保留可编辑内容与样式，下载后可在演示文稿软件中查看。</p>}
    </>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
