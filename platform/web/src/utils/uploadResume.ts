/** Metadata only: file bytes and credentials never enter browser storage. */
export const UPLOAD_CHUNK = 4 * 1024 * 1024;
export const UPLOAD_INLINE = 16 * 1024 * 1024;
export const UPLOAD_MAX = 1024 * 1024 * 1024;
export interface FileVersion {path:string; exists:boolean; content_sha256:string|null; size:number; protected:boolean}
export interface UploadStatus {id:string; path:string; size:number; offset:number; status?:string; sha256?:string|null}
export interface SourceFile {name:string; size:number; blocks:string[]}
export interface ResumeRecord {
  schema:1; scope:string; clientId:string; path:string; version:FileVersion;
  source:SourceFile; upload:UploadStatus|null;
  serverRecovered?:true;
  keyedCreation?:true;
}
const DB = "vf-upload-resume", TABLE = "uploads";
const HASH = /^[a-f0-9]{64}$/;
export const resumeScope = (userId:number,caseId:number) => `${userId}:${caseId}`;
export class InvalidResumeRecord extends Error {
  constructor() {super("本机上传记录已损坏，无法安全恢复。可清除这条记录，再检查目标文件。");}
}
const storageError = () => new Error("无法保存本机上传进度。请允许此网站使用浏览器存储，再重试大文件上传。");
async function database():Promise<IDBDatabase> {
  return new Promise((resolve,reject)=>{
    let request:IDBOpenDBRequest;
    try {request=indexedDB.open(DB,1);} catch {reject(storageError());return;}
    request.onupgradeneeded=()=>{request.result.createObjectStore(TABLE,{keyPath:"scope"});};
    request.onerror=()=>reject(storageError());
    request.onblocked=()=>reject(storageError());
    request.onsuccess=()=>{request.result.onversionchange=()=>request.result.close();resolve(request.result);};
  });
}
/** Atomic read/change: a stale tab cannot recreate a cancelled record or replace another upload. */
async function transaction<T>(scope:string,change:(value:unknown,store:IDBObjectStore)=>T,write=false):Promise<T> {
  const db=await database();
  return new Promise((resolve,reject)=>{
    let result:T,problem:unknown;
    const tx=db.transaction(TABLE,write?"readwrite":"readonly"),store=tx.objectStore(TABLE);
    const request=store.get(scope);
    request.onsuccess=()=>{try {result=change(request.result,store);} catch(cause) {problem=cause;tx.abort();}};
    tx.oncomplete=()=>{db.close();resolve(result);};
    tx.onabort=tx.onerror=()=>{db.close();reject(problem??storageError());};
  });
}
export function validateUpload(value:UploadStatus,record:ResumeRecord):UploadStatus {
  if (!value || !/^[a-f0-9]{32}$/.test(value.id) || value.path!==record.path || value.size!==record.source.size ||
      (record.upload && value.id!==record.upload.id) || !Number.isInteger(value.offset) || value.offset<0 || value.offset>value.size ||
      (value.status!==undefined && !["uploading","completed"].includes(value.status)) ||
      (value.status==="completed" && (value.offset!==value.size || !HASH.test(value.sha256??""))))
    throw new Error("服务端上传记录与本机文件不匹配。请取消这次上传后重新检查目标。");
  return value;
}
function validate(value:unknown,scope:string):ResumeRecord {
  const r=value as ResumeRecord;
  if (!r || r.schema!==1 || r.scope!==scope || typeof r.clientId!=="string" || !/^[a-f0-9-]{36}$/.test(r.clientId) ||
      typeof r.path!=="string" || !r.path || r.path.length>4096 || !r.version || r.version.path!==r.path ||
      typeof r.version.exists!=="boolean" || r.version.protected!==false ||
      (r.version.exists ? !HASH.test(r.version.content_sha256??"") : r.version.content_sha256!==null) ||
      !Number.isSafeInteger(r.version.size) || r.version.size<0 || !r.source || typeof r.source.name!=="string" ||
      (r.serverRecovered!==undefined && r.serverRecovered!==true) ||
      (r.keyedCreation!==undefined && r.keyedCreation!==true) ||
      !Number.isSafeInteger(r.source.size) || r.source.size<=(r.serverRecovered?0:UPLOAD_INLINE) || r.source.size>UPLOAD_MAX ||
      !Array.isArray(r.source.blocks) || r.source.blocks.length!==Math.ceil(r.source.size/UPLOAD_CHUNK) ||
      r.source.blocks.some(h=>typeof h!=="string" || !HASH.test(h))) throw new InvalidResumeRecord();
  if (r.upload!==null) {try {validateUpload(r.upload,r);} catch {throw new InvalidResumeRecord();}}
  return r;
}
export const readResume = (scope:string) => transaction(scope,value=>value===undefined?null:validate(value,scope));
export const saveResume = (record:ResumeRecord,create=false) => transaction(record.scope,(value,store)=>{
  if (create ? value!==undefined : !value || (value as ResumeRecord).clientId!==record.clientId)
    throw new Error("另一窗口已更新或取消上传。请刷新页面，读取最新进度。");
  const previous=value as ResumeRecord|undefined;
  if(previous?.upload && previous.upload.id!==record.upload?.id)
    throw new Error("另一窗口已建立上传，请刷新页面后继续同一次上传。");
  validate(record,record.scope);store.put(record);
},true);
export const removeResume = (record:ResumeRecord) => transaction(record.scope,(value,store)=>{
  if ((value as ResumeRecord|undefined)?.clientId===record.clientId) store.delete(record.scope);
},true);
/** Only the explicit corrupt-record recovery button may remove an unidentifiable record. */
export const removeInvalidResume = (scope:string) => transaction(scope,(value,store)=>{
  if (value===undefined) return;
  try {validate(value,scope);} catch(cause) {if(cause instanceof InvalidResumeRecord) {store.delete(scope);return;}throw cause;}
  throw new Error("另一窗口已更新上传记录，请刷新页面。");
},true);
export async function chunkHash(blob:Blob):Promise<string> {
  const hash=await crypto.subtle.digest("SHA-256",await blob.arrayBuffer());
  return Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,"0")).join("");
}
export class UploadPaused extends Error {}
export async function identifyFile(file:File,stopped:()=>boolean,progress:(bytes:number)=>void,expected?:SourceFile,serverRecovered=false):Promise<SourceFile> {
  if (file.size<=(serverRecovered?0:UPLOAD_INLINE) || file.size>UPLOAD_MAX) throw new Error(serverRecovered
    ? "续传文件大小必须为1字节至1 GiB。" : "续传文件大小必须大于 16 MiB，且不超过 1 GiB。");
  if (expected && file.size!==expected.size) throw new Error("所选文件与原文件大小不同，请重新选择原文件。");
  const blocks:string[]=[];
  for (let start=0;start<file.size;start+=UPLOAD_CHUNK) {
    if (stopped()) throw new UploadPaused();
    const hash=await chunkHash(file.slice(start,start+UPLOAD_CHUNK));
    if (expected && hash!==expected.blocks[blocks.length]) throw new Error("所选文件内容与原文件不同，请重新选择原文件。已上传的部分仍保留。");
    blocks.push(hash);progress(Math.min(start+UPLOAD_CHUNK,file.size));
  }
  if (stopped()) throw new UploadPaused();
  return {name:file.name,size:file.size,blocks};
}
