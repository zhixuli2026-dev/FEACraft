import assert from 'node:assert/strict';
import { createPdfRangeTransport } from '../web/src/utils/pdfRange.ts';

const version = 'a'.repeat(64);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const checks = [];
function fixture() {
  const delivered=[], errors=[];
  let destroyed=0;
  class Transport { onDataRange(begin, data) { delivered.push({begin, data:[...data]}); } }
  const range=createPdfRangeTransport({PDFDataRangeTransport:Transport}, {
    url:'/api/v1/cases/1/raw?path=sample.pdf',token:'test',length:8,version,
    onError:error=>errors.push(error.message),
  });
  const task={destroy(){destroyed++;return Promise.resolve();}};
  return {range,task,delivered,errors,get destroyed(){return destroyed;}};
}
function response({status=206,token=version,contentRange='bytes 0-3/8',length='4',body=[1,2,3,4]}={}) {
  return new Response(new Uint8Array(body),{status,headers:{
    'X-Vibeflow-File-Version':token,'Content-Range':contentRange,'Content-Length':length}});
}
let f=fixture();
globalThis.fetch=async (_url,options)=>{
  assert.equal(options.headers['X-Vibeflow-File-Version'],version);
  assert.equal(options.headers.Range,'bytes=0-3');
  assert.equal(options.headers.Authorization,'Bearer test');
  return response();
};
f.range.setLoadingTask(f.task);f.range.transport.requestDataRange(0,4);await tick();
assert.deepEqual(f.delivered,[{begin:0,data:[1,2,3,4]}]);assert.equal(f.errors.length,0);
checks.push('conditional_request_delivers_exact_verified_range');
for (const [name,options] of [
  ['changed_file',{status:409}], ['wrong_version',{token:'b'.repeat(64)}],
  ['missing_version',{token:''}], ['wrong_offset',{contentRange:'bytes 4-7/8'}],
  ['wrong_total',{contentRange:'bytes 0-3/9'}], ['wrong_length',{length:'8'}],
  ['short_body',{body:[1,2]}], ['full_body_fallback',{status:200}],
]) {
  f=fixture();f.range.setLoadingTask(f.task);
  globalThis.fetch=async()=>response(options);
  f.range.transport.requestDataRange(0,4);await tick();
  assert.equal(f.delivered.length,0,name);assert.equal(f.errors.length,1,name);
  assert.equal(f.destroyed,1,name);checks.push(name+'_stops_without_delivery');
}
f=fixture();const requests=[];
globalThis.fetch=(_url,options)=>new Promise(resolve=>requests.push({resolve,signal:options.signal}));
f.range.setLoadingTask(f.task);
f.range.transport.requestDataRange(0,4);f.range.transport.requestDataRange(4,8);
requests[0].resolve(response({status:409}));await tick();
assert.ok(requests[1].signal.aborted,'Sibling not aborted');
requests[1].resolve(response({contentRange:'bytes 4-7/8'}));await tick();
f.range.transport.requestDataRange(0,4);
assert.equal(requests.length,2);assert.equal(f.delivered.length,0);assert.equal(f.errors.length,1);
checks.push('failure_aborts_siblings_and_rejects_late_or_new_chunks');
f=fixture();globalThis.fetch=async()=>response({status:409});
f.range.transport.requestDataRange(0,4);await tick();f.range.setLoadingTask(f.task);
assert.equal(f.destroyed,1);checks.push('early_failure_destroys_task_when_attached');
f=fixture();f.range.setLoadingTask(f.task);
globalThis.fetch=()=>{throw new Error('Invalid range must not fetch');};
f.range.transport.requestDataRange(0,9);
assert.equal(f.errors.length,1);assert.equal(f.destroyed,1);
checks.push('invalid_pdf_range_rejected_before_request');
f=fixture();let complete;
globalThis.fetch=()=>new Promise(resolve=>{complete=resolve;});
f.range.transport.requestDataRange(0,4);f.range.abort();complete(response());await tick();
assert.equal(f.delivered.length,0);assert.equal(f.errors.length,0);
checks.push('normal_cleanup_silently_ignores_late_response');
console.log('PDF_RANGE_CONSISTENCY_OK '+JSON.stringify({count:checks.length,checks}));
