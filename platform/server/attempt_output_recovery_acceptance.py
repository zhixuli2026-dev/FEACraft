"""Actual native output + S3 partial upload + API exit; no LLM exercised."""
import argparse,asyncio,concurrent.futures,contextlib,hashlib,json,os,re,shutil,socket,sqlite3,subprocess,sys,tempfile,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import SERVER,IsolatedServer
from s3testsupport import ObjectStore

FIXTURE=r'''
import asyncio,contextlib,hashlib,json,os,threading,tracemalloc
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Case,DurableEvent,Job,JobAttempt,OutputArchiveReservation,NativeRequest,JobLogReservation,ArtifactPublication,ArtifactPublicationHead
from app.routers.jobs import _get_job
from app.services import attempt_objects as objects,jobrunner,process_identity_native as identity
from app.services import attempt_output_recovery as recovery
from app.services import attempt_cache_recovery as cache_recovery
from app.services import job_execution
from app.services import job_output_finalizing as full_recovery
from app.services.async_ops import finish_before_cancel
root=Path(__file__).parent
original_full_recovery=full_recovery.recover
async def actual_full_recovery(*args,**kwargs):
 try:return await original_full_recovery(*args,**kwargs)
 except BaseException as failure:
  import traceback
  chain=[];current=failure
  while current is not None and len(chain)<6:
   chain.append({'type':type(current).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(current.__traceback__)]})
   current=current.__cause__
  (root/'actual-full-recovery-error-frames.json').write_text(json.dumps(chain,indent=2),encoding='utf8')
  raise
full_recovery.recover=actual_full_recovery
pause_copy=False;pause_cache=False;copy_ready=threading.Event();copy_release=threading.Event();copy_task=None
original_output_stream=objects.output_stream
@contextlib.contextmanager
def actual_output_stream(prefix,path,size):
 with original_output_stream(prefix,path,size) as chunks:
  def forwarded():
   for chunk in chunks:
    yield chunk
    if pause_cache and path.endswith('/run/A-output.bin'):
     copy_ready.set();copy_release.wait()
  yield forwarded()
objects.output_stream=actual_output_stream
original_restore=cache_recovery.restore
async def actual_restore(*args,**kwargs):
 global copy_task
 if pause_cache:copy_task=asyncio.current_task()
 return await original_restore(*args,**kwargs)
cache_recovery.restore=actual_restore
@app.post('/api/v1/test/output-arm-cache-cancel')
async def arm_cache_cancel(user=Depends(get_current_user)):
 global pause_cache,copy_task
 pause_cache=True;copy_task=None;copy_ready.clear();copy_release.clear();return {'armed':True}
commit_stage=None
cache_stage=None
terminal_stage=None
original_install_cache=objects.install_runtime_cache
original_move_member=objects.move_file_new
from app.services.private_cache import Cache
original_fetch_many=Cache.fetch_many
def observed_fetch_many(self,items,**kwargs):
 if (root/'observe-partial-output-fetch').exists():
  items=list(items)
  with (root/'actual-partial-output-fetch.jsonl').open('a',encoding='utf8') as trace:
   trace.write(json.dumps({name:size for name,size,_ in items})+'\n')
 return original_fetch_many(self,items,**kwargs)
Cache.fetch_many=observed_fetch_many
cache_thread=threading.local()
def cache_boundary():
 with (root/'actual-cache-install-boundary.json').open('x',encoding='utf8') as stream:
  json.dump({'stage':cache_stage},stream);stream.flush();os.fsync(stream.fileno())
 threading.Event().wait()
def actual_move_member(source,destination,**kwargs):
 original_move_member(source,destination,**kwargs)
 if cache_stage=='partial-member' and destination.as_posix().endswith('/inputs/Ref/output-config.txt'):
  cache_boundary()
 if cache_stage=='partial-output-member' and destination.as_posix().endswith('/run/A-output.bin'):
  cache_boundary()
objects.move_file_new=actual_move_member
def actual_install_cache(*args,**kwargs):
 if cache_stage=='before':cache_boundary()
 if (root/'cache-rebind.armed').exists():
  view=args[3]
  target=objects.workspace.checked_path(view.root,'A-output.bin')
  assert target.resolve().is_relative_to((root/'files').resolve())
  data=target.read_bytes();stamp=target.stat()
  (root/'cache-rebind-original.bin').write_bytes(data)
  target.write_bytes(bytes([data[0]^1])+data[1:])
  os.utime(target,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
  assert target.stat().st_size==stamp.st_size and target.stat().st_mtime_ns==stamp.st_mtime_ns
 cache_thread.installing=True
 try:
  value=original_install_cache(*args,**kwargs)
 finally:
  cache_thread.installing=False
 if cache_stage=='after':cache_boundary()
 return value
objects.install_runtime_cache=actual_install_cache
original_digest_file=objects.workspace.digest_file
def actual_digest_file(path,*args,**kwargs):
 observation=root/'cache-body-observation.armed'
 if path.name=='B-output.bin' and observation.exists():
  from fault_fixture import FileBarrier
  phase='install' if getattr(cache_thread,'installing',False) else 'prepare'
  with (root/'actual-cache-body-phases.jsonl').open('a',encoding='utf8') as log:
   log.write(json.dumps({'phase':phase,'bytes':path.stat().st_size})+'\n')
  barrier=FileBarrier(root,'cache-body-read',timeout=20)
  if phase==observation.read_text(encoding='utf8') and not barrier.ready.exists():barrier.wait()
 return original_digest_file(path,*args,**kwargs)
objects.workspace.digest_file=actual_digest_file
@app.post('/api/v1/test/output-arm-cache-install')
async def arm_cache_install(body:dict,user=Depends(get_current_user)):
 global cache_stage
 assert body['stage'] in {'before','after','partial-member','partial-output-member'}
 cache_stage=body['stage'];return {'stage':cache_stage}
from sqlalchemy.ext.asyncio import AsyncSession
original_commit=AsyncSession.commit
original_attempt_event=job_execution.attempt_event
async def actual_attempt_event(db,job,attempt,event_type,payload):
 value=await original_attempt_event(db,job,attempt,event_type,payload)
 if commit_stage is not None and event_type==recovery.RECOVERED:db.info['actual_output_recovery_commit']=value.id
 if terminal_stage is not None and event_type=='jobs.updated' and payload.get('status') in {'succeeded','failed','cancelled','interrupted'}:
  db.info['actual_full_finalization_commit']={'event_id':value.id,'status':payload['status'],'attempt_id':attempt.id}
 return value
job_execution.attempt_event=actual_attempt_event
async def commit_boundary(identifier):
 with (root/'actual-output-metadata-boundary.json').open('x',encoding='utf8') as stream:
  json.dump({'stage':commit_stage,'recovery_event_id':identifier},stream);stream.flush();os.fsync(stream.fileno())
 await asyncio.Event().wait()
async def actual_commit(db):
 identifier=db.info.get('actual_output_recovery_commit')
 terminal=db.info.get('actual_full_finalization_commit')
 async def final_boundary():
  with (root/'actual-full-finalization-boundary.json').open('x',encoding='utf8') as stream:
   json.dump({'stage':terminal_stage,**terminal},stream);stream.flush();os.fsync(stream.fileno())
  await asyncio.Event().wait()
 if terminal is not None and terminal_stage=='before':await final_boundary()
 if identifier is not None and commit_stage=='before':await commit_boundary(identifier)
 value=await original_commit(db)
 if terminal is not None and terminal_stage=='after':await final_boundary()
 if identifier is not None and commit_stage=='after':await commit_boundary(identifier)
 return value
AsyncSession.commit=actual_commit
@app.post('/api/v1/test/output-arm-terminal')
async def arm_terminal(body:dict,user=Depends(get_current_user)):
 global terminal_stage
 assert body['stage'] in {'before','after'}
 terminal_stage=body['stage'];return {'stage':terminal_stage}
@app.post('/api/v1/test/output-arm-commit')
async def arm_commit(body:dict,user=Depends(get_current_user)):
 global commit_stage
 assert body['stage'] in {'before','after'}
 commit_stage=body['stage'];return {'stage':commit_stage}
@app.post('/api/v1/test/output-drop-run')
async def drop_run(body:dict,user=Depends(get_current_user)):
 import tempfile
 from app import storage
 from app.services import attempt_workspace as workspace
 from app.services.output_archive_storage import ready
 async with SessionLocal() as db:
  job=await _get_job(body['job_id'],user,db);case=await db.get(Case,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  prefix=case.s3_prefix;input_json=attempt.input_manifest_json;output_json=attempt.output_manifest_json
 output=json.loads(output_json);run=storage._fs_path(prefix,output['run_path']).resolve(strict=True)
 assert run.is_relative_to(root.resolve()) and run.name=='run'
 assert await asyncio.to_thread(ready,prefix,input_json,output_json)
 directories=[];actual=await asyncio.to_thread(workspace.inventory,run,sorted(p.name for p in run.iterdir()),directories)
 assert actual==output['files'] and directories==output['directories']
 partial_output=bool(body.get('partial_outputs'))
 members=[workspace.checked_path(run,name) for name in ('A-output.bin','Ref/output-producer.py','empty/leaf')] if partial_output else [run]
 if body.get('whole_attempt') or body.get('partial_inputs'):
  target=run.parent
  assert target.name==attempt.id and target.resolve().is_relative_to(root.resolve())
  manifest=json.loads(input_json)['workspace']
  await asyncio.to_thread(objects.sealed_input_inventory,prefix,manifest)
  await asyncio.to_thread(objects.verify_members,prefix,manifest['inputs_path'],manifest['files'])
  await asyncio.to_thread(workspace.verify_inputs,prefix,manifest)
  if body.get('whole_attempt'):
   members=[target]
  else:
   assert 'Ref/output-config.txt' in manifest['files']
   members.extend([workspace.checked_path(target,'inputs/Ref/output-config.txt'),
                   workspace.checked_path(target,'manifest.json')])
 # Preserve the detached test files and any finalization journals in the
 # isolated evidence directory while exercising absence at the runtime path.
 def detach():
  archived=Path(tempfile.mkdtemp(prefix='detached-owned-cache-',dir=root))
  for member in members:
   assert member.resolve().is_relative_to(root.resolve())
   member.rename(archived/member.name)
  assert all(not member.exists() for member in members)
  return archived.name
 detached=await asyncio.to_thread(detach)
 return {'actual_owned_run_removed':not partial_output,'whole_attempt':bool(body.get('whole_attempt')),
   'partial_inputs':bool(body.get('partial_inputs')),'partial_outputs':partial_output,'detached':detached}
@app.get('/api/v1/test/output-cache-state/{identifier}')
async def cache_state(identifier:int,user=Depends(get_current_user)):
 from app import storage
 from app.services import attempt_workspace as workspace
 async with SessionLocal() as db:
  job=await _get_job(identifier,user,db);case=await db.get(Case,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  output=json.loads(attempt.output_manifest_json)
  events=(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type=='attempt.output_cache_restored'))).all()
  run=storage._fs_path(case.s3_prefix,output['run_path'])
  prefix=case.s3_prefix
 directories=[];paths=sorted(p.name for p in run.iterdir()) if run.exists() else []
 files=await asyncio.to_thread(workspace.inventory,run,paths,directories) if paths else {}
 return {'run_exists':run.exists(),'attempt_exists':run.parent.exists(),
  'inputs_exists':(run.parent/'inputs').is_dir(),'manifest_exists':(run.parent/'manifest.json').is_file(),
  'files':files,'directories':directories,
  'cache_events':[{'id':e.id,'payload':json.loads(e.payload_json)} for e in events]}
@app.post('/api/v1/test/output-cache-corrupt')
async def cache_corrupt(body:dict,user=Depends(get_current_user)):
 from app import storage
 from app.services import attempt_workspace as workspace
 async with SessionLocal() as db:
  job=await _get_job(body['job_id'],user,db);case=await db.get(Case,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  run=storage._fs_path(case.s3_prefix,json.loads(attempt.output_manifest_json)['run_path'])
 target=workspace.checked_path(run,'A-output.bin');original=root/'actual-cache-original-output.bin'
 assert target.resolve().is_relative_to(root.resolve())
 if body['action']=='corrupt':
  with original.open('xb') as saved:saved.write(target.read_bytes())
  target.write_bytes(b'actual deliberately corrupted owned cache')
 else:
  assert body['action']=='restore';target.write_bytes(original.read_bytes())
 return {'action':body['action']}
original_recover=recovery.recover
async def actual_recover(*args,**kwargs):
 global copy_task
 if pause_copy:copy_task=asyncio.current_task()
 return await original_recover(*args,**kwargs)
recovery.recover=actual_recover
original_put_output=objects.put_output_member
def measured_put_output(prefix,base,name,source,expected):
 if name!='B-output.bin':
  return original_put_output(prefix,base,name,source,expected)
 tracemalloc.start()
 try:return original_put_output(prefix,base,name,source,expected)
 finally:
  _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
  with (root/'actual-large-output-allocation.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'pid':os.getpid(),'size':expected['size'],'peak_traced_bytes':peak})+'\n')
objects.put_output_member=measured_put_output
original_create_output=objects._create_immutable
def actual_create_output(prefix,path,data):
 if path.endswith('/run/B-output.bin'):
  from app import storage
  before=data.tell();data.seek(0,os.SEEK_END);size=data.tell();data.seek(before)
  claims=(storage._fs_root()/'.node-storage-budget').glob('*.json')
  reserved=any(json.loads(claim.read_text(encoding='utf8')).get('bytes')==size for claim in claims)
  assert reserved
  (root/'actual-large-output-claim.json').write_text(json.dumps({'bytes':size,'live_during_put':reserved}),encoding='utf8')
 value=original_create_output(prefix,path,data)
 marker=root/'actual-output-upload-boundary.json'
 professional=root/'native-handler'
 native_target=professional.exists() and ((professional.read_text()=='report' and path.endswith('/E-report/build/report.html')) or (professional.read_text()=='latex' and path.endswith('.pdf')))
 if '/run/' in path and (path.endswith('/A-output.bin') or native_target) and not marker.exists() and not (root/'normal-mode').exists():
  if isinstance(data,bytes):
   size=len(data);digest=hashlib.sha256(data).hexdigest()
  else:
   original=data.tell();data.seek(0);size=0;hasher=hashlib.sha256()
   for block in iter(lambda:data.read(1024*1024),b''):
    size+=len(block);hasher.update(block)
   data.seek(original);digest=hasher.hexdigest()
  with marker.open('x',encoding='utf8') as stream:
   json.dump({'path':path,'size':size,'sha256':digest},stream);stream.flush();os.fsync(stream.fileno())
  threading.Event().wait()
 if pause_copy and (path.endswith('/run/Ref/output-producer.py') or (professional.exists() and '/run/' in path)):
  copy_ready.set();copy_release.wait()
 return value
objects._create_immutable=actual_create_output
@app.post('/api/v1/test/output-arm-cancel')
async def arm_cancel(user=Depends(get_current_user)):
 global pause_copy,copy_task
 pause_copy=True;copy_task=None;copy_ready.clear();copy_release.clear();return {'armed':True}
@app.get('/api/v1/test/output-cancel-status')
async def cancel_status(user=Depends(get_current_user)):return {'entered':copy_ready.is_set(),'task_done':copy_task.done() if copy_task else False}
@app.post('/api/v1/test/output-cancel-now')
async def cancel_now(user=Depends(get_current_user)):
 assert copy_ready.is_set() and copy_task is not None and not copy_task.done()
 accepted=[]
 for _ in range(3):accepted.append(copy_task.cancel());await asyncio.sleep(.005)
 assert not copy_task.done(),'Cancellation abandoned the actual blocked IO thread'
 copy_release.set()
 try:await finish_before_cancel(copy_task)
 except asyncio.CancelledError:pass
 assert copy_task.done()
 return {'accepted':accepted,'actual_thread_drained':True}
@app.post('/api/v1/test/output-local-fault')
async def local_fault(body:dict,user=Depends(get_current_user)):
 from app import storage
 from app.services.attempt_workspace import checked_path
 async with SessionLocal() as db:
  job=await _get_job(body['job_id'],user,db);case=await db.get(Case,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  run=storage._fs_path(case.s3_prefix,json.loads(attempt.input_manifest_json)['workspace']['run_path'])
  source=run/'Ref/output-producer.py';hidden=root/'hidden-output-source.py'
  assert source.resolve().is_relative_to(root.resolve()) and hidden.resolve().is_relative_to(root.resolve())
  if body['action']=='hide':checked_path(run,'Ref/output-producer.py').rename(hidden)
  else:
   assert body['action']=='restore' and hidden.is_file() and not source.exists();hidden.rename(source)
  return {'action':body['action']}
@app.get('/api/v1/test/output-process')
async def process(user=Depends(get_current_user)):return identity.observe(os.getpid())
@app.post('/api/v1/test/output-exit')
async def exit_now(user=Depends(get_current_user)):os._exit(79)
@app.get('/api/v1/test/output-state/{identifier}')
async def state(identifier:int,user=Depends(get_current_user)):
 from datetime import timezone
 async with SessionLocal() as db:
  job=await _get_job(identifier,user,db)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  reservation=await db.get(OutputArchiveReservation,attempt.id)
  events=(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
   DurableEvent.type.in_((recovery.DRAINED,recovery.RECOVERED,'attempt.cancelled_archive_copy_closed'))).order_by(DurableEvent.seq))).all()
  terminal_events=(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
   DurableEvent.type.in_(('jobs.updated','attempt.finalizing_recovery_drained'))).order_by(DurableEvent.seq))).all()
  native_requests=(await db.scalars(select(NativeRequest).where(NativeRequest.case_id==job.case_id,NativeRequest.kind=='job.finalize'))).all()
  related_requests={r.id for r in native_requests if json.loads(r.profile_json).get('job_id')==job.id}
  finalizer_events=(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==job.case_id,
   DurableEvent.type.like('job.finalizer%')).order_by(DurableEvent.seq))).all()
  finalizer_evidence=[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in finalizer_events
   if json.loads(e.payload_json).get('request_id',json.loads(e.payload_json).get('binding',{}).get('request_id')) in related_requests]
  log=await db.get(JobLogReservation,job.id)
  reserved=json.loads(attempt.publication_json).get('publication_id')
  publication=await db.get(ArtifactPublication,reserved) if reserved else None
  publication_head=await db.get(ArtifactPublicationHead,(job.case_id,publication.kind)) if publication else None
  from app.models import CaseStorageOperation
  io_operations=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==job.case_id,CaseStorageOperation.journal_id==attempt.id)))
  io_events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type.like('job.finalizing_io_%')).order_by(DurableEvent.seq)))
  changes=(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type=='attempt.state_changed'))).all()
  claims=[e for e in changes if json.loads(e.payload_json).get('status')=='claimed'];assert len(claims)==1
  at=claims[0].created_at
  if at.tzinfo is None:at=at.replace(tzinfo=timezone.utc)
  deadline=at.timestamp()+json.loads(attempt.input_manifest_json)['execution_spec']['timeout_seconds']
  proc=jobrunner.get_proc(job.id)
  return {'job_id':job.id,'job_status':job.status,'attempt_id':attempt.id,'phase':attempt.phase,'reason_code':attempt.reason_code,'version':attempt.version,'original_deadline_unix':deadline,'log_pointer':job.log_s3_key,
   'input':json.loads(attempt.input_manifest_json),'output':json.loads(attempt.output_manifest_json),'write_checkpoint':json.loads(attempt.write_checkpoint_json),
   'actual_in_memory_stop':proc._owned_process.stop_result if proc and proc._owned_process else None,
   'reservation':{'state':reservation.state,'size':reservation.size,'remaining_bytes':reservation.remaining_bytes} if reservation else None,
   'copy_events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in events],
   'terminal_events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in terminal_events],
   'finalization_requests':[{'id':r.id,'phase':r.phase,'error':r.error,'owner_identity':json.loads(r.owner_identity_json),'profile':json.loads(r.profile_json),'children':json.loads(r.children_json),'result':json.loads(r.result_json)} for r in native_requests if json.loads(r.profile_json).get('job_id')==job.id],
   'finalizer_evidence':finalizer_evidence,
   'publication_state':{'id':publication.id,'status':publication.status,'current_id':publication_head.active_publication_id if publication_head else None} if publication else None,
   'job_status_events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in await db.scalars(select(DurableEvent).where(DurableEvent.case_id==job.case_id,DurableEvent.type=='jobs.updated')) if json.loads(e.payload_json).get('job_id')==job.id],
   'finalizing_io_operations':[{'id':r.id,'state':r.state,'owner':json.loads(r.owner_json),'plan_sha256':r.plan_sha256,'event_id':r.event_id} for r in io_operations],
   'finalizing_io_evidence':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in io_events],
   'remote_log':{'state':log.state,'size':log.size,'sha256':log.sha256,'remaining_bytes':log.remaining_bytes} if log else None}
'''
SOURCES=['attempt_output_recovery_acceptance.py','testsupport.py','s3testsupport.py','app/services/attempt_output_recovery.py','app/services/attempt_cache_recovery.py','app/services/job_output_finalizing.py','app/services/job_finalizing_source.py','app/services/native_request_records.py','app/services/native_request_execution.py','app/services/job_write_checkpoint.py','app/services/publication_finalizing.py','app/routers/jobs.py','app/services/attempt_workspace.py','app/services/attempt_objects.py','app/services/output_archive_storage.py','app/services/jobrunner.py']
SOURCES += ['app/services/finalizing_native_scope.py','app/services/finalizing_native_usage.py','app/services/finalizing_request_recovery.py',
 'app/services/execution_budget.py','app/services/execution_output_checkpoint.py','app/services/native_diagnostics.py','app/services/native_execution.py']
SOURCES += ['app/services/finalizing_native_preparation.py']
SOURCES += ['app/services/finalizing_request_cancellation.py']
SOURCES += ['app/services/finalizing_job_cancellation.py']
SOURCES += ['app/services/finalizing_io.py','app/services/storage_operations.py','app/services/job_log_storage.py']
SOURCES += ['app/services/cancelled_archive_recovery.py']
SOURCES += ['app/services/finalizing_stage.py','app/services/fs_directory.py']
SOURCES += ['app/services/publication_alias_recovery.py']
SOURCES += ['app/services/file_transactions.py','app/services/node_storage_budget.py','app/services/request_deadline.py',
            'app/services/request_lifecycle.py']

def native_script(large_output_mib=0):
    script = "from pathlib import Path\nimport os,sys\nwith Path(sys.argv[1]).open('a',encoding='utf8') as log:log.write('actual native invocation\\n')\nPath('A-output.bin').write_bytes(os.urandom(262144))\nPath('empty/leaf').mkdir(parents=True)\n"
    if large_output_mib:
        script += f"with Path('B-output.bin').open('xb') as member:\n    for _ in range({large_output_mib}):\n        member.write(os.urandom(1024*1024))\n"
    return script + "print('actual output written')\n"


def output_object(store, state, name):
    manifest = state['input']['workspace']
    path = manifest['storage']['prefix'] + '/' + manifest['run_path'] + '/' + name
    return {'Bucket': store.bucket, 'Key': path}


def verify_large_member(store, api, state, size_mib):
    record = state['output']['files']['B-output.bin']
    assert record['size'] == size_mib * 1024 * 1024
    response = store.raw_client.get_object(**output_object(store, state, 'B-output.bin'))
    digest = hashlib.sha256()
    size = 0
    with contextlib.closing(response['Body']) as body:
        for block in iter(lambda: body.read(1024 * 1024), b''):
            size += len(block)
            digest.update(block)
    assert size == record['size'] and digest.hexdigest() == record['sha256']
    records = [json.loads(line) for line in
               (api.root/'actual-large-output-allocation.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(records) == 1 and records[0]['size'] == record['size'], records
    assert records[0]['peak_traced_bytes'] < 12 * 1024 * 1024, records
    claim = json.loads((api.root/'actual-large-output-claim.json').read_text(encoding='utf8'))
    assert claim == {'bytes': size, 'live_during_put': True}
    live = (api.root/'files'/'.node-storage-budget').glob('*.json')
    assert not any(json.loads(claim.read_text(encoding='utf8')).get('bytes') == size for claim in live)
    return {'size': size, 'sha256': digest.hexdigest(), 'allocation': records[0],
            'node_claim': claim, 'node_claim_released': True, 'physical_object_read_verified': True}

def verify_normal(*,database_url=None,large_output_mib=0):
    from app.services import process_identity_native as identity
    api=IsolatedServer('attempt-output-normal',app='output_fixture:app',database_url=database_url)
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                      volume_max=24 if large_output_mib else 3)
    report={'passed':False,'normal_completion':True,'database':'postgresql' if database_url else 'sqlite','model_calls':0,'mock_llm_calls':0,'API_births':[],'developer_database_modified':False}
    (api.root/'normal-mode').write_text('Actual no-fault production completion\n',encoding='utf8')
    print('ATTEMPT_OUTPUT_NORMAL '+str(api.root)+' STORE '+str(store.root),flush=True)
    try:
        with store:
            (api.root/'output_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,encoding='utf8')
            with api:
                api.create_case('Actual ordinary S3 completion');report['API_births'].append(api.call('GET','/test/output-process'))
                counter=api.root/'actual-native-invocations.txt';api.put('Ref/output-producer.py',native_script(large_output_mib))
                command=subprocess.list2cmdline([sys.executable,'-X','utf8','Ref/output-producer.py',str(counter)])
                job=api.call('POST',api.prefix+'/jobs',{'type':'shell','command':command,'runtime':'windows','timeout_seconds':60,'input_paths':['Ref/output-producer.py']})
                final=api.wait_job(job['id'],timeout=120)
                state=api.call('GET','/test/output-state/'+str(job['id']));report.update(job=final,state=state)
                assert final['status']=='succeeded' and state['phase']=='succeeded' and state['write_checkpoint']['state']=='stopped'
                assert state['write_checkpoint']['stop']['verified'] and state['output']['process_stop']==state['output']['output_preparation']['stop']
                assert state['output']['object_store']['state']=='complete' and state['reservation']['state']=='materialized' and state['reservation']['remaining_bytes']==0
                assert not any(e['type']=='attempt.outputs_recovered' for e in state['copy_events'])
                assert counter.read_text(encoding='utf8').splitlines()==['actual native invocation']
                if large_output_mib:
                    report['large_output'] = verify_large_member(store, api, state, large_output_mib)
                report['functional_passed']=True
    except BaseException as error:report['failure']={'type':type(error).__name__,'detail':str(error)[:1500]}
    finally:
        report['API_exit_probes']=[]
        for birth in report['API_births']:
            until=time.monotonic()+10
            while True:
                value=identity.probe(birth)
                if value['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.03)
            report['API_exit_probes'].append(value)
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        report['passed']=bool(report.get('functional_passed') and report['store_stopped'] and all(v['state']=='exited' for v in report['API_exit_probes']))
        report['source_hashes']={p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in SOURCES}
        (api.root/'output-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('ATTEMPT_OUTPUT_NORMAL_RESULT '+str(api.root/'output-result.json'),flush=True)
    return report['passed'],api.root

def verify(*,before_fix=False,repairable_fault=False,cancel_copy=False,two_apis=False,database_url=None,metadata_boundary=None,drop_local_run=False,restore_cache=False,cold_attempt_cache=False,cache_crash=None,cancel_cache=False,finalize_outputs=False,terminal_boundary=None,expire_finalize=False,large_output_mib=0,cache_lock_probe=None,partial_input_cache=None,partial_output_cache=None):
    from app.services import process_identity_native as identity
    if metadata_boundary and (cancel_copy or two_apis):raise ValueError('Use a separate actual run for metadata crash and cancellation/peer cases')
    if drop_local_run and not metadata_boundary:raise ValueError('Cache loss is exercised only after a verified complete archive boundary')
    if (cold_attempt_cache or cache_crash or cancel_cache) and not restore_cache:raise ValueError('Cache restore options require explicit cache acceptance')
    if terminal_boundary and not finalize_outputs:raise ValueError('Terminal crash requires full finalization acceptance')
    if expire_finalize and (not finalize_outputs or terminal_boundary):raise ValueError('Use explicit expired full finalization without a success commit crash')
    if cache_lock_probe and (not restore_cache or not large_output_mib or database_url or two_apis or cancel_cache or cache_crash):
        raise ValueError('Cache lock probe needs the standalone SQLite large-output restore')
    if partial_input_cache and (not restore_cache or cold_attempt_cache or cache_lock_probe or cancel_cache or two_apis):
        raise ValueError('Partial cache check needs the standalone explicit restore')
    if cache_crash=='partial-member' and partial_input_cache!='restore':
        raise ValueError('Partial member crash requires actual partial input recovery')
    if partial_output_cache and (not restore_cache or cold_attempt_cache or cache_lock_probe or cancel_cache):
        raise ValueError('Partial output cache check needs explicit restore')
    if cache_crash=='partial-output-member' and partial_output_cache!='restore':
        raise ValueError('Partial output member crash requires actual partial output recovery')
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                      volume_max=24 if large_output_mib else 3,authenticated=bool(cache_lock_probe or cache_crash or partial_input_cache or partial_output_cache))
    api=IsolatedServer('attempt-output-recovery',app='output_fixture:app',database_url=database_url)
    report={'passed':False,'before_fix':before_fix,'repairable_fault':repairable_fault,'cancel_copy':cancel_copy,'two_APIs':two_apis,'metadata_boundary':metadata_boundary,'drop_local_run':drop_local_run,'database':'postgresql' if database_url else 'sqlite','model_calls':0,'mock_llm_calls':0,'kind':'actual_native_SQL_HTTP_S3','checks':[],'API_births':[],'developer_database_modified':False}
    report.update(restore_cache=restore_cache,cold_attempt_cache=cold_attempt_cache,cache_crash=cache_crash,cancel_cache=cancel_cache)
    report.update(finalize_outputs=finalize_outputs,terminal_boundary=terminal_boundary,expire_finalize=expire_finalize)
    def save():
        report['source_hashes']={p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in SOURCES}
        (api.root/'output-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def raw(method,path,payload=None,*,base=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
            r=client.request(method,path,json=payload)
            try:value=r.json()
            except ValueError:value={'body':r.text[:1000]}
            return r.status_code,value
    def call(method,path,payload=None,expect=200,**kwargs):
        status,value=raw(method,path,payload,**kwargs)
        assert status==expect,(method,path,status,str(value)[:1000]);return value

    def observed_cache_install(path,payload):
        from fault_fixture import FileBarrier
        barrier=FileBarrier(api.root,'cache-body-read')
        armed=api.root/'cache-body-observation.armed'
        armed.write_text(cache_lock_probe,encoding='utf8')
        uri=(api.root/'test.sqlite').resolve().as_uri()+'?mode=rw'
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(call,'POST',path,payload)
            try:
                deadline=time.monotonic()+75
                while not barrier.ready.exists():
                    assert not pending.done(),pending.result()
                    if time.monotonic()>=deadline:raise TimeoutError('Archive cache body read never reached its real checkpoint')
                    time.sleep(.05)
                started=time.monotonic()
                with contextlib.closing(sqlite3.connect(uri,uri=True,timeout=.8)) as writer:
                    try:
                        writer.execute('CREATE TABLE cache_budget_witness (id INTEGER PRIMARY KEY)')
                        writer.commit()
                        concurrent_write=True
                    except sqlite3.OperationalError as error:
                        if str(error)!='database is locked':raise
                        concurrent_write=False
                report['cache_lock_probe']={'read_phase':cache_lock_probe,'independent_sqlite_write':concurrent_write,
                    'write_seconds':round(time.monotonic()-started,3)}
                assert concurrent_write is (cache_lock_probe=='prepare'),report['cache_lock_probe']
            finally:
                barrier.release()
            ready=pending.result(timeout=90)
        armed.unlink()
        trace=[json.loads(line) for line in (api.root/'actual-cache-body-phases.jsonl').read_text(encoding='utf8').splitlines()]
        report['cache_lock_probe']['locked_body_bytes']=sum(row['bytes'] for row in trace if row['phase']=='install')
        assert (report['cache_lock_probe']['locked_body_bytes']==0) is (cache_lock_probe=='prepare')
        return ready
    def exited(birth):
        until=time.monotonic()+10
        while True:
            found=identity.probe(birth)
            if found['state']=='exited':return found
            assert time.monotonic()<until,found;time.sleep(.03)
    @contextlib.contextmanager
    def peer():
        if not two_apis:yield api.base;return
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port);report['peer_api']=base;report.setdefault('peer_apis',[]).append(base)
        with (api.root/'peer.log').open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','output_fixture:app','--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            birth=None
            try:
                until=time.monotonic()+45
                with httpx.Client(trust_env=False,timeout=1) as client:
                    while True:
                        assert proc.poll() is None
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        assert time.monotonic()<until,'Peer API did not become healthy'
                        time.sleep(.1)
                birth=call('GET','/test/output-process',base=base);report['API_births'].append(birth);save();yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                if birth:exited(birth)
    print('ATTEMPT_OUTPUT_RECOVERY '+str(api.root)+' STORE '+str(store.root),flush=True);save()
    try:
        with store:
            (api.root/'output_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,encoding='utf8')
            with api:
                api.create_case('Actual pending output recovery');user=api.call('GET','/auth/me')
                report['API_births'].append(call('GET','/test/output-process'));save()
                counter=api.root/'actual-native-invocations.txt'
                api.put('Ref/output-producer.py',native_script(large_output_mib))
                inputs=['Ref/output-producer.py']
                if partial_input_cache or partial_output_cache:
                    api.put('Ref/output-config.txt','original archived file content\n')
                    inputs.append('Ref/output-config.txt')
                command=subprocess.list2cmdline([sys.executable,'-X','utf8','Ref/output-producer.py',str(counter)])
                job=call('POST',api.prefix+'/jobs',{'type':'shell','command':command,'runtime':'windows','timeout_seconds':15 if expire_finalize else 300 if finalize_outputs else 60,'input_paths':inputs})
                report['job_id']=job['id'];save()
                marker=api.root/'actual-output-upload-boundary.json';until=time.monotonic()+90
                while not marker.exists():
                    value=call('GET','/jobs/'+str(job['id']));assert value['status'] not in {'succeeded','failed','cancelled','interrupted'},value
                    assert time.monotonic()<until,'Actual S3 upload did not reach its boundary';time.sleep(.1)
                captured=call('GET','/test/output-state/'+str(job['id']));report['before_exit']=captured;save()
                assert captured['phase']=='finalizing' and captured['output']['object_store']['state']=='pending'
                assert captured['actual_in_memory_stop']['verified'] and captured['actual_in_memory_stop']['root_exit_code']==0
                assert counter.read_text(encoding='utf8').splitlines()==['actual native invocation']
                if large_output_mib:
                    from botocore.exceptions import ClientError
                    original = output_object(store, captured, 'A-output.bin')
                    retained = store.raw_client.head_object(**original)['ETag']
                    try:
                        store.raw_client.head_object(**output_object(store, captured, 'B-output.bin'))
                    except ClientError as error:
                        assert error.response['ResponseMetadata']['HTTPStatusCode'] == 404
                    else:
                        raise AssertionError('The recovery member already existed before the publisher exited')
                    report['actual_partial_archive'] = {'prior_member_present': True, 'large_member_missing': True}
                if before_fix:
                    assert captured['write_checkpoint']=={} and 'process_stop' not in captured['output'] and 'output_preparation' not in captured['output']
                    report['checks'].append({'check':'actual_successful_native_stop_not_persisted_before_real_output_upload'})
                else:assert captured['output']['output_preparation']['stop']['verified']
                try:call('POST','/test/output-exit')
                except httpx.TransportError:pass
                else:raise AssertionError('Actual API exit returned HTTP')
                exited(report['API_births'][-1]);api.__exit__(RuntimeError,None,None)
                shutil.copyfile(api.root/'api.log',api.root/'original-owner.log')
                api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
                report['API_births'].append(call('GET','/test/output-process'));save()
                state=call('GET','/test/output-state/'+str(job['id']));report['after_restart']=state
                assert state['attempt_id']==captured['attempt_id'] and state['output']==captured['output']
                if before_fix:
                    call('GET','/jobs/'+str(job['id'])+'/output-recovery',expect=404)
                    report['checks'].append({'check':'actual_restart_retains_pending_bytes_but_has_no_output_recovery_entry'})
                else:
                    options=call('GET','/jobs/'+str(job['id'])+'/output-recovery')
                    report['options']=options;save()
                    payload={'attempt_id':state['attempt_id'],'inventory_sha256':options['inventory_sha256']}
                    call('POST','/jobs/'+str(job['id'])+':recover-outputs',{**payload,'inventory_sha256':'0'*64},expect=409)
                    call('POST','/jobs/'+str(job['id'])+':recover-outputs',{**payload,'attempt_id':str(uuid.uuid4())},expect=409)
                    if repairable_fault:
                        call('POST','/test/output-local-fault',{'job_id':job['id'],'action':'hide'})
                        failure=call('POST','/jobs/'+str(job['id'])+':recover-outputs',payload,expect=409)
                        assert failure['detail']['code']=='output_recovery_unavailable';report['actual_missing_source_refused']=failure;save()
                        call('POST','/test/output-local-fault',{'job_id':job['id'],'action':'restore'})
                    if metadata_boundary:
                        call('POST','/test/output-arm-commit',{'stage':metadata_boundary})
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pending=pool.submit(raw,'POST','/jobs/'+str(job['id'])+':recover-outputs',payload)
                            marker=api.root/'actual-output-metadata-boundary.json';until=time.monotonic()+45
                            while not marker.exists():
                                assert not pending.done(),pending.result()
                                assert time.monotonic()<until;time.sleep(.05)
                            reached=json.loads(marker.read_text(encoding='utf8'));assert reached['stage']==metadata_boundary
                            if drop_local_run:report['actual_cache_loss']=call('POST','/test/output-drop-run',{'job_id':job['id']})
                            try:call('POST','/test/output-exit')
                            except httpx.TransportError:pass
                            else:raise AssertionError('Actual metadata API exit returned HTTP')
                            report['metadata_owner_exit']=exited(report['API_births'][-1])
                            try:pending.result()
                            except httpx.TransportError:pass
                            else:raise AssertionError('Killed metadata response unexpectedly completed')
                        api.__exit__(RuntimeError,None,None);shutil.copyfile(api.root/'api.log',api.root/('metadata-owner-'+metadata_boundary+'.log'))
                        api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
                        report['API_births'].append(call('GET','/test/output-process'));report['actual_metadata_crash']=reached;save()
                    with peer() as base:
                        if cancel_copy:
                            call('POST','/test/output-arm-cancel')
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                pending=pool.submit(raw,'POST','/jobs/'+str(job['id'])+':recover-outputs',payload)
                                until=time.monotonic()+30
                                while not call('GET','/test/output-cancel-status')['entered']:
                                    assert not pending.done(),pending.result()
                                    assert time.monotonic()<until;time.sleep(.05)
                                if two_apis:
                                    busy=call('POST','/jobs/'+str(job['id'])+':recover-outputs',payload,expect=409,base=base)
                                    assert busy['detail']['code'] in {'output_archive_busy','storage_quota_busy'};report['actual_peer_busy']=busy
                                report['actual_cancellation']=call('POST','/test/output-cancel-now')
                                cancelled=pending.result()
                                assert cancelled[0]==503 and cancelled[1]['detail']['code']=='request_interrupted',cancelled
                            report['cancelled_HTTP_status']=cancelled[0]
                            report['cancelled_HTTP_detail']=cancelled[1]['detail']
                            after_cancel=call('GET','/test/output-state/'+str(job['id']));report['after_actual_cancel']=after_cancel;save()
                            assert after_cancel['output']['object_store']['state']=='pending'
                            assert any(e['type']=='attempt.output_copy_drained' and e['payload']['outcome']=='cancelled' for e in after_cancel['copy_events'])
                        if two_apis:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                                results=[f.result() for f in [pool.submit(raw,'POST','/jobs/'+str(job['id'])+':recover-outputs',payload,base=at) for at in (api.base,base)]]
                            report['actual_two_API_results']=results;save();assert any(v[0]==200 for v in results),results
                            assert all(v[0] in {200,409} for v in results),results
                            assert all(v[0]!=409 or v[1]['detail']['code'] in {'storage_quota_busy','output_archive_busy','state_conflict'} for v in results),results
                            result=call('POST','/jobs/'+str(job['id'])+':recover-outputs',payload,base=base)
                            assert all(v[1]==result for v in results if v[0]==200),results
                        else:result=call('POST','/jobs/'+str(job['id'])+':recover-outputs',payload)
                    report['recovered']=result;save()
                    assert result['state']=='archived' and result['attempt_id']==captured['attempt_id']
                    if metadata_boundary=='after':assert result['recovery_event_id']==reached['recovery_event_id']
                    elif metadata_boundary=='before':assert result['recovery_event_id']!=reached['recovery_event_id']
                    assert call('POST','/jobs/'+str(job['id'])+':recover-outputs',{'attempt_id':state['attempt_id'],'inventory_sha256':options['inventory_sha256']})==result
                    assert counter.read_text(encoding='utf8').splitlines()==['actual native invocation']
                    final=call('GET','/test/output-state/'+str(job['id']));report['final_state']=final
                    assert final['reservation']['state']=='materialized' and final['reservation']['remaining_bytes']==0
                    assert sum(e['type']=='attempt.outputs_recovered' for e in final['copy_events'])==1
                    assert final['job_status']=='running' and final['phase']=='finalizing' and result['finalization_pending']
                    if large_output_mib:
                        report['large_output'] = verify_large_member(store, api, final, large_output_mib)
                        assert store.raw_client.head_object(**original)['ETag'] == retained
                        report['large_output']['prior_member_version_retained'] = True
                    report['checks'].append({'check':'actual_same_attempt_output_upload_resumes_without_native_reexecution','actual_missing_file_repair_and_same_API_retry':repairable_fault,'actual_three_cancellations':cancel_copy,'actual_two_APIs':two_apis,'actual_metadata_commit_crash':metadata_boundary,'actual_archived_local_cache_loss':drop_local_run})
                    if restore_cache:
                        report['actual_cache_loss']=call('POST','/test/output-drop-run',{'job_id':job['id'],
                            'whole_attempt':cold_attempt_cache,'partial_inputs':bool(partial_input_cache),
                            'partial_outputs':bool(partial_output_cache)})
                        missing=call('GET','/test/output-cache-state/'+str(job['id']));assert missing['run_exists'] is bool(partial_output_cache)
                        assert missing['inputs_exists'] is not cold_attempt_cache
                        assert missing['manifest_exists'] is not bool(cold_attempt_cache or partial_input_cache)
                        report['cache_state_missing']=missing;save()
                        cache_path='/jobs/'+str(job['id'])+':restore-output-cache'
                        call('POST',cache_path,{**payload,'inventory_sha256':'0'*64},expect=409)
                        call('POST',cache_path,{**payload,'attempt_id':str(uuid.uuid4())},expect=409)
                        if partial_input_cache:
                            manifest=final['input']['workspace'];relative=manifest['inputs_path']
                            inputs_root=api.root/'files'/manifest['storage']['prefix']/relative
                            missing_member=inputs_root/'Ref/output-config.txt'
                            missing_manifest=inputs_root.parent/'manifest.json'
                            retained=inputs_root/'Ref/output-producer.py'
                            known=retained.read_bytes();inode=retained.stat().st_ino
                            assert not missing_member.exists() and not missing_manifest.exists()
                            if partial_input_cache=='baseline':
                                denial=call('POST',cache_path,payload,expect=409)
                                assert not missing_member.exists() and not missing_manifest.exists()
                                report['partial_input_baseline']={'rejection':denial['detail'],'original_gaps_preserved':True}
                                detached=api.root/report['actual_cache_loss']['detached']
                                assert detached.resolve().is_relative_to(api.root.resolve())
                                (detached/missing_member.name).rename(missing_member)
                                (detached/missing_manifest.name).rename(missing_manifest)
                            else:
                                retained.write_bytes(bytes([known[0]^1])+known[1:])
                                try:
                                    call('POST',cache_path,payload,expect=409)
                                    assert not missing_member.exists() and not missing_manifest.exists()
                                    assert retained.read_bytes()!=known
                                finally:retained.write_bytes(known)
                                address={'Bucket':store.bucket,'Key':manifest['storage']['prefix']+'/'+relative+'/Ref/output-config.txt'}
                                original=b'original archived file content\n'
                                changed=bytes([original[0]^1])+original[1:]
                                store.raw_client.put_object(**address,Body=changed)
                                try:
                                    call('POST',cache_path,payload,expect=409)
                                    assert not missing_member.exists() and not missing_manifest.exists()
                                    with contextlib.closing(store.raw_client.get_object(**address)['Body']) as body:
                                        assert body.read()==changed
                                finally:store.raw_client.put_object(**address,Body=original)
                        if partial_output_cache:
                            manifest=final['input']['workspace']
                            run=api.root/'files'/manifest['storage']['prefix']/final['output']['run_path']
                            assert run.resolve().is_relative_to(api.root.resolve())
                            original_run_inode=run.stat().st_ino
                            gaps=[run/name for name in ('A-output.bin','Ref/output-producer.py','empty/leaf')]
                            assert all(not path.exists() for path in gaps)
                            output_survivor=run/'Ref/output-config.txt'
                            survivor_bytes=output_survivor.read_bytes();survivor_inode=output_survivor.stat().st_ino
                            if partial_output_cache=='baseline':
                                denial=call('POST',cache_path,payload,expect=409)
                                assert all(not path.exists() for path in gaps)
                                report['partial_output_baseline']={'rejection':denial['detail'],'gaps_preserved':True}
                                detached=api.root/report['actual_cache_loss']['detached']
                                assert detached.resolve().is_relative_to(api.root.resolve())
                                for path in gaps:(detached/path.name).rename(path)
                            else:
                                altered=bytes([survivor_bytes[0]^1])+survivor_bytes[1:]
                                output_survivor.write_bytes(altered)
                                try:
                                    call('POST',cache_path,payload,expect=409)
                                    assert all(not path.exists() for path in gaps) and output_survivor.read_bytes()==altered
                                finally:output_survivor.write_bytes(survivor_bytes)
                                extra=run/'unregistered-local.bin'
                                with extra.open('xb') as unknown:unknown.write(b'local unregistered data')
                                try:
                                    call('POST',cache_path,payload,expect=409)
                                    assert all(not path.exists() for path in gaps) and extra.read_bytes()==b'local unregistered data'
                                finally:extra.unlink()
                                address=output_object(store,final,'A-output.bin')
                                with contextlib.closing(store.raw_client.get_object(**address)['Body']) as stream:
                                    original=stream.read()
                                changed=bytes([original[0]^1])+original[1:]
                                store.raw_client.put_object(**address,Body=changed)
                                try:
                                    call('POST',cache_path,payload,expect=409)
                                    assert all(not path.exists() for path in gaps)
                                finally:store.raw_client.put_object(**address,Body=original)
                                (api.root/'observe-partial-output-fetch').touch()
                        if cache_crash:
                            call('POST','/test/output-arm-cache-install',{'stage':cache_crash})
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                pending=pool.submit(raw,'POST',cache_path,payload)
                                marker=api.root/'actual-cache-install-boundary.json';until=time.monotonic()+45
                                while not marker.exists():
                                    assert not pending.done(),pending.result()
                                    assert time.monotonic()<until,'Actual cache recovery did not reach its boundary';time.sleep(.05)
                                reached=json.loads(marker.read_text(encoding='utf8'));assert reached['stage']==cache_crash
                                observed=call('GET','/test/output-cache-state/'+str(job['id']))
                                assert observed['run_exists']==(bool(partial_output_cache) or cache_crash=='after') and not observed['cache_events']
                                if cache_crash=='partial-member':
                                    assert missing_member.read_bytes()==b'original archived file content\n'
                                    assert not missing_manifest.exists()
                                    reached.update(input_member_installed=True,input_manifest_absent=True)
                                if cache_crash=='partial-output-member':
                                    assert gaps[0].is_file() and not gaps[1].exists()
                                    reached.update(first_output_member_installed=True,second_output_member_absent=True)
                                    if not database_url:
                                        uri=(api.root/'test.sqlite').resolve().as_uri()+'?mode=rw'
                                        started=time.monotonic()
                                        with contextlib.closing(sqlite3.connect(uri,uri=True,timeout=.8)) as writer:
                                            writer.execute('CREATE TABLE partial_output_io_witness (id INTEGER PRIMARY KEY)')
                                            writer.commit()
                                        reached.update(independent_sqlite_write=True,write_seconds=round(time.monotonic()-started,3))
                                report['actual_cache_install_crash']=reached;report['cache_state_before_crash']=observed;save()
                                try:call('POST','/test/output-exit')
                                except httpx.TransportError:pass
                                else:raise AssertionError('Actual cache API exit returned HTTP')
                                exited(report['API_births'][-1])
                                try:pending.result()
                                except httpx.TransportError:pass
                                else:raise AssertionError('Killed cache response unexpectedly completed')
                            api.__exit__(RuntimeError,None,None);shutil.copyfile(api.root/'api.log',api.root/('cache-owner-'+cache_crash+'.log'))
                            api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
                            report['API_births'].append(call('GET','/test/output-process'));save()
                        if cancel_cache:
                            call('POST','/test/output-arm-cache-cancel')
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                pending=pool.submit(raw,'POST',cache_path,payload);until=time.monotonic()+30
                                while not call('GET','/test/output-cancel-status')['entered']:
                                    assert not pending.done(),pending.result()
                                    assert time.monotonic()<until;time.sleep(.05)
                                report['actual_cache_cancellation']=call('POST','/test/output-cancel-now')
                                cancelled=pending.result()
                                assert cancelled[0]==503 and cancelled[1]['detail']['code']=='request_interrupted',cancelled
                            after_cancel=call('GET','/test/output-cache-state/'+str(job['id']))
                            assert not after_cancel['run_exists'] and not after_cancel['cache_events']
                            report['cache_cancelled_HTTP_status']=cancelled[0]
                            report['cache_cancelled_HTTP_detail']=cancelled[1]['detail'];save()
                        if two_apis:
                            with peer() as base:
                                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                                    results=[f.result() for f in [pool.submit(raw,'POST',cache_path,payload,base=at) for at in (api.base,base)]]
                                report['actual_cache_two_API_results']=results;save()
                                assert any(v[0]==200 for v in results) and all(v[0] in {200,409} for v in results),results
                                assert all(v[0]!=409 or v[1]['detail']['code'] in {'storage_quota_busy','state_conflict','output_recovery_busy'} for v in results),results
                                ready=call('POST',cache_path,payload,base=base)
                                assert all(v[1]==ready for v in results if v[0]==200),results
                        elif cache_lock_probe:ready=observed_cache_install(cache_path,payload)
                        else:ready=call('POST',cache_path,payload)
                        assert ready['state']=='cache_ready' and ready['finalization_pending'] and not ready['native_execution_started']
                        assert ready['archive_event_id']==result['recovery_event_id']
                        restored=call('GET','/test/output-cache-state/'+str(job['id']))
                        assert restored['inputs_exists'] and restored['manifest_exists']
                        if partial_output_cache=='restore':
                            assert all(path.exists() for path in gaps)
                            assert run.stat().st_ino==original_run_inode
                            assert output_survivor.read_bytes()==survivor_bytes and output_survivor.stat().st_ino==survivor_inode
                            fetches=[json.loads(line) for line in (api.root/'actual-partial-output-fetch.jsonl').read_text(encoding='utf8').splitlines()]
                            expected={name:final['output']['files'][name]['size'] for name in ('A-output.bin','Ref/output-producer.py')}
                            assert fetches[0]==expected,fetches
                            if cache_crash=='partial-output-member':
                                assert fetches[1:]==[{'Ref/output-producer.py':expected['Ref/output-producer.py']}],fetches
                            else:assert len(fetches)==1,fetches
                            report['partial_output_cache']={'two_files_and_empty_directory_restored':True,
                                'corrupt_survivor_preserved_and_refused':True,'surviving_member_same_inode':True,
                                'unexpected_file_retained':True,'corrupt_missing_remote_member_refused':True,
                                'run_directory_same_inode':True,'actual_staged_member_bytes':fetches}
                        if partial_input_cache=='restore':
                            assert retained.read_bytes()==known and retained.stat().st_ino==inode
                            assert missing_member.read_bytes()==b'original archived file content\n'
                            assert json.loads(missing_manifest.read_text(encoding='utf8'))==manifest
                            report['partial_input_cache']={'missing_member_and_manifest_restored':True,
                                'corrupt_survivor_preserved_and_refused':True,'intact_survivor_same_inode':True,
                                'corrupt_remote_object_preserved_and_refused':True}
                        assert restored['files']==final['output']['files'] and restored['directories']==final['output']['directories']
                        assert 'empty/leaf' in restored['directories'] and len(restored['cache_events'])==1
                        assert call('POST',cache_path,payload)==ready
                        if cache_lock_probe=='prepare':
                            armed=api.root/'cache-rebind.armed';armed.touch()
                            try:
                                rejected=call('POST',cache_path,payload,expect=409)
                                assert rejected['detail']['code']=='output_cache_unavailable'
                            finally:armed.unlink()
                            rebind=call('GET','/test/output-cache-state/'+str(job['id']))
                            current=rebind['files']['A-output.bin'];old=restored['files']['A-output.bin']
                            assert current['size']==old['size'] and current['sha256']!=old['sha256']
                            assert rebind['cache_events']==restored['cache_events']
                            target=api.root/'files'/final['input']['workspace']['storage']['prefix']/final['output']['run_path']/'A-output.bin'
                            assert target.resolve().is_relative_to((api.root/'files').resolve())
                            target.write_bytes((api.root/'cache-rebind-original.bin').read_bytes())
                            assert call('POST',cache_path,payload)==ready
                            report['cache_lock_probe']['same_size_same_mtime_change_refused_without_overwrite']=True
                        call('POST','/test/output-cache-corrupt',{'job_id':job['id'],'action':'corrupt'})
                        call('POST',cache_path,payload,expect=409)
                        damaged=call('GET','/test/output-cache-state/'+str(job['id']))
                        assert damaged['files']['A-output.bin']['size']==len(b'actual deliberately corrupted owned cache')
                        call('POST','/test/output-cache-corrupt',{'job_id':job['id'],'action':'restore'})
                        assert call('POST',cache_path,payload)==ready
                        unchanged=call('GET','/test/output-state/'+str(job['id']))
                        assert unchanged['output']==final['output'] and unchanged['input']==final['input'] and unchanged['write_checkpoint']=={}
                        assert unchanged['job_status']=='running' and unchanged['phase']=='finalizing'
                        assert counter.read_text(encoding='utf8').splitlines()==['actual native invocation']
                        report.update(cache_ready=ready,cache_state_restored=restored)
                        report['checks'].append({'check':'actual_original_runtime_cache_restored_and_corruption_retained','cold_attempt_cache':cold_attempt_cache,'actual_install_crash':cache_crash,'actual_download_cancel':cancel_cache,'actual_two_APIs':two_apis,'native_invocations':1,'empty_directories_verified':True});save()
                    if finalize_outputs:
                        if expire_finalize:
                            while time.time()<=captured['original_deadline_unix']+.05:time.sleep(.05)
                            report['actual_original_deadline_expired_at_request']=time.time();save()
                        full_path='/jobs/'+str(job['id'])+':finalize-outputs'
                        call('POST',full_path,{**payload,'attempt_id':str(uuid.uuid4())},expect=409)
                        call('POST',full_path,{**payload,'inventory_sha256':'0'*64},expect=409)
                        if terminal_boundary:
                            call('POST','/test/output-arm-terminal',{'stage':terminal_boundary})
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                pending=pool.submit(raw,'POST',full_path,payload)
                                marker=api.root/'actual-full-finalization-boundary.json';until=time.monotonic()+90
                                while not marker.exists():
                                    assert not pending.done(),pending.result()
                                    assert time.monotonic()<until,'Full finalization did not reach actual SQL commit';time.sleep(.05)
                                reached=json.loads(marker.read_text(encoding='utf8'))
                                assert reached['stage']==terminal_boundary and reached['status']=='succeeded' and reached['attempt_id']==captured['attempt_id']
                                report['actual_terminal_crash']=reached;save()
                                try:call('POST','/test/output-exit')
                                except httpx.TransportError:pass
                                else:raise AssertionError('Actual full-finalization API exit returned HTTP')
                                exited(report['API_births'][-1])
                                try:pending.result()
                                except httpx.TransportError:pass
                                else:raise AssertionError('Killed terminal response unexpectedly completed')
                            api.__exit__(RuntimeError,None,None);shutil.copyfile(api.root/'api.log',api.root/('terminal-owner-'+terminal_boundary+'.log'))
                            api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
                            report['API_births'].append(call('GET','/test/output-process'));save()
                        completed=call('POST',full_path,payload)
                        expected_status='failed' if expire_finalize else 'succeeded'
                        assert completed['state']==expected_status and completed['exit_code']==(-1 if expire_finalize else 0) and not completed['finalization_pending'] and not completed['native_execution_started']
                        assert call('POST',full_path,payload)==completed
                        done=call('GET','/test/output-state/'+str(job['id']))
                        assert done['attempt_id']==captured['attempt_id'] and done['job_status']==expected_status and done['phase']==expected_status
                        assert done['input']==captured['input'] and done['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5'
                        assert done['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                        assert len(done['finalization_requests'])==1 and done['finalization_requests'][0]['phase']=='succeeded'
                        if expire_finalize:
                            assert done['log_pointer']==done['output']['run_path']+'/.vibeflow/finalizing/root.log' and done['remote_log'] is None
                            log=call('GET','/jobs/'+str(job['id'])+'/log')
                            assert hashlib.sha256(log['chunk'].encode()).hexdigest()==captured['output']['finalizing_source']['log']['sha256']
                        else:
                            assert done['remote_log']['state']=='materialized' and done['remote_log']['remaining_bytes']==0
                            assert {k:done['remote_log'][k] for k in ('size','sha256')}==captured['output']['finalizing_source']['log']
                        assert sum(e['type']=='jobs.updated' and e['payload'].get('status')==expected_status for e in done['terminal_events'])==1
                        if expire_finalize:
                            assert done['reason_code']=='resource_limit' and done['finalization_requests'][0]['result']['usage']['violation']=='timeout'
                            assert done['original_deadline_unix']==captured['original_deadline_unix']
                        assert counter.read_text(encoding='utf8').splitlines()==['actual native invocation']
                        report.update(full_finalization=completed,full_final_state=done)
                        report['checks'].append({'check':'actual_original_job_full_terminal_recovery_without_native_reexecution','actual_terminal_crash':terminal_boundary,'same_attempt':True,'original_log_preserved':True,'expired_original_deadline_not_renewed':expire_finalize});save()
                report['functional_passed']=True
    except BaseException as error:report['failure']={'type':type(error).__name__,'detail':str(error)[:1500],'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        report['API_exit_probes']=[exited(b) for b in report['API_births']]
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        report['passed']=bool(report.get('functional_passed') and report['store_stopped']);save()
    print('ATTEMPT_OUTPUT_RECOVERY_RESULT '+str(api.root/'output-result.json'),flush=True)
    return report['passed'],api.root

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--before-fix',action='store_true');parser.add_argument('--repairable-local-fault',action='store_true');parser.add_argument('--cancel-copy',action='store_true');parser.add_argument('--two-apis',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--metadata-boundary',choices=['before','after']);parser.add_argument('--drop-local-run',action='store_true');parser.add_argument('--normal-completion',action='store_true');parser.add_argument('--restore-cache',action='store_true');parser.add_argument('--cold-attempt-cache',action='store_true');parser.add_argument('--cache-crash',choices=['before','after','partial-member','partial-output-member']);parser.add_argument('--cancel-cache',action='store_true');parser.add_argument('--finalize-outputs',action='store_true');parser.add_argument('--terminal-boundary',choices=['before','after']);parser.add_argument('--expire-finalize',action='store_true');parser.add_argument('--verify-migration',action='store_true')
    parser.add_argument('--large-output-mib',type=int,choices=range(33,65),default=0)
    parser.add_argument('--cache-lock-probe',choices=['install','prepare'])
    parser.add_argument('--partial-input-cache',choices=['baseline','restore'])
    parser.add_argument('--partial-output-cache',choices=['baseline','restore'])
    args=parser.parse_args()
    async def run(url=None):
        if args.normal_completion:return await asyncio.to_thread(verify_normal,database_url=url,large_output_mib=args.large_output_mib)
        return await asyncio.to_thread(verify,before_fix=args.before_fix,repairable_fault=args.repairable_local_fault,cancel_copy=args.cancel_copy,two_apis=args.two_apis,database_url=url,metadata_boundary=args.metadata_boundary,drop_local_run=args.drop_local_run,restore_cache=args.restore_cache,cold_attempt_cache=args.cold_attempt_cache,cache_crash=args.cache_crash,cancel_cache=args.cancel_cache,finalize_outputs=args.finalize_outputs,terminal_boundary=args.terminal_boundary,expire_finalize=args.expire_finalize,large_output_mib=args.large_output_mib,cache_lock_probe=args.cache_lock_probe,partial_input_cache=args.partial_input_cache,partial_output_cache=args.partial_output_cache)
    if not args.postgres:return 0 if (await run())[0] else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url);assert selected.drivername=='postgresql+asyncpg' and selected.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='output-recovery-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
    print('OUTPUT_RECOVERY_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');passed,api=await run(url);report.update(passed=False if args.verify_migration else passed,native_acceptance_passed=passed,api_root=str(api))
        if passed and args.verify_migration:
            proof=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database=name,timeout=10)
            try:
                tables=['jobs','job_attempts','durable_events','job_log_reservations','native_requests']
                async def fingerprints():
                    return {t:hashlib.sha256(json.dumps(sorted(r['row'] for r in await proof.fetch('SELECT row_to_json(t)::text AS row FROM '+t+' t'))).encode()).hexdigest() for t in tables}
                before=await fingerprints();assert await proof.fetchval('SELECT version_num FROM alembic_version')=='a08c65df2197'
                if args.finalize_outputs:
                    try:await asyncio.to_thread(alembic,url,root,'downgrade','f9d4a10735bc')
                    except AssertionError:
                        assert 'Downgrade would discard original-Job recovery validation/drain evidence' in (root/'alembic.log').read_text()
                    else:raise AssertionError('Downgrade discarded complete recovery evidence')
                    report['migration_downgrade_blocked_by_actual_recovery']=True
                else:
                    await asyncio.to_thread(alembic,url,root,'downgrade','f9d4a10735bc')
                    assert await proof.fetchval('SELECT version_num FROM alembic_version')=='f9d4a10735bc'
                    assert await fingerprints()==before
                    await asyncio.to_thread(alembic,url,root,'upgrade','head');report['migration_old_data_roundtrip_preserved']=True
                assert await proof.fetchval('SELECT version_num FROM alembic_version')=='a08c65df2197' and await fingerprints()==before
                report['migration_row_fingerprints']=before
            finally:await proof.close()
        report['passed']=passed
        return 0 if passed else 1
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
