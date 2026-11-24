"""Actual stopped Pandoc, early-stage interruption and same-attempt recovery."""
import argparse,asyncio,concurrent.futures,contextlib,hashlib,json,os,socket,subprocess,sys,tempfile,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import SERVER,IsolatedServer
from s3testsupport import ObjectStore
from attempt_output_recovery_acceptance import FIXTURE,SOURCES
from attempt_professional_finalizing_acceptance import EXTRA

EARLY=r'''
from app.services import finalizing_stage as early_stages
from app.services import finalizing_stage_recovery as early_recovery
actual_target_attempt=None
actual_early_recovery_tasks={}
actual_early_recovery_release=threading.Event()
original_early_acquire=early_recovery.acquire
async def actual_early_acquire(*args,**kwargs):
 global actual_target_attempt
 value=await original_early_acquire(*args,**kwargs);actual_target_attempt=value.owner.attempt_id;return value
early_recovery.acquire=actual_early_acquire
original_early_recover=early_recovery.recover
async def actual_early_recover(*args,**kwargs):
 actual_early_recovery_tasks[args[0]]=asyncio.current_task()
 return await original_early_recover(*args,**kwargs)
early_recovery.recover=actual_early_recover
original_early_capture=early_recovery.capture
original_early_resume_objects=recovery.resume_objects
def observed_early_resume_objects(*args,**kwargs):
 if (root/'closed-archive-peers').exists():
  with (root/'actual-archive-copy-invocations.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'actual_resume_objects_called':True,'pid':os.getpid()})+'\n');stream.flush();os.fsync(stream.fileno())
 return original_early_resume_objects(*args,**kwargs)
recovery.resume_objects=observed_early_resume_objects
async def actual_early_capture(*args,**kwargs):
 value=await original_early_capture(*args,**kwargs)
 marker=root/'actual-early-recovery-boundary.json';mode=root/'recovery-boundary'
 if mode.exists() and mode.read_text()=='capture_commit' and not marker.exists():
  stage=args[0];actual_mixed_marker(marker,{'boundary':'capture_commit','operation_id':stage.lease.id,'stage_sha256':stage.sha256,'actual_capture_returned':True,'response':value})
  await asyncio.Event().wait()
 return value
early_recovery.capture=actual_early_capture
@app.get('/api/v1/test/early-recovery-task/{identifier}')
async def observed_early_task(identifier:int,user=Depends(get_current_user)):
 task=actual_early_recovery_tasks.get(identifier)
 return {'cancelling':bool(task and (task.cancelling() or task.cancelled())),'done':bool(task and task.done()),'available_request_slots':early_recovery.outputs.SLOTS._value}
@app.post('/api/v1/test/early-recovery-release/{identifier}')
async def release_early_thread(identifier:int,user=Depends(get_current_user)):
 task=actual_early_recovery_tasks[identifier];assert not task.done()
 actual_early_recovery_release.set()
 try:await finish_before_cancel(task)
 except asyncio.CancelledError:pass
 assert task.done();return {'actual_thread_joined':True,'actual_request_done':True}
@app.get('/api/v1/test/early-recovery-evidence/{identifier}')
async def early_evidence(identifier:int,user=Depends(get_current_user)):
 from app.models import CaseStorageOperation
 async with SessionLocal() as db:
  job=await _get_job(identifier,user,db)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
   DurableEvent.type.in_((early_recovery.IO_STARTED,early_recovery.IO_DRAINED,early_recovery.IO_EXITED,early_recovery.CAPTURED,early_stages.PLANNED,early_stages.INSTALLED))).order_by(DurableEvent.seq)))
  rows=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==job.case_id,CaseStorageOperation.journal_id==attempt.id)))
  return {'events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in events],
   'operations':[{'id':r.id,'state':r.state,'owner':json.loads(r.owner_json),'stage_sha256':r.plan_sha256} for r in rows]}
original_early_begin=early_stages.begin
async def actual_early_begin(*args,**kwargs):
 global actual_target_attempt
 value=await original_early_begin(*args,**kwargs)
 selected=value is not None and ((root/'native-handler').read_text()!='solve' or value.value['evidence_input'] is not None)
 if selected:actual_target_attempt=value.owner.attempt_id
 if selected and (root/'early-boundary').read_text()=='before' and not (root/'actual-early-boundary.json').exists():
  actual_mixed_marker(root/'actual-early-boundary.json',{'boundary':'before','stage_sha256':value.sha256,'actual_original_stop_recorded':True})
  await asyncio.Event().wait()
 return value
early_stages.begin=actual_early_begin
original_early_append=early_stages.append_bytes
original_early_append_log=early_stages.append_log
def early_partial(stream,data,offset):
 if isinstance(data,early_stages.LogSource):
  full=data.record['size'];count=min(7,full-offset)
  with data.path.open('rb') as source:
   source.seek(offset);part=source.read(count)
 else:
  full=len(data);count=min(7,full-offset);part=data[offset:offset+count]
 stream.seek(offset);stream.write(part);stream.flush();os.fsync(stream.fileno())
 return count,full
def actual_early_append_source(stream,data,offset,original):
 if actual_target_attempt is not None and actual_target_attempt in Path(stream.name).parts and (root/'early-boundary').read_text()=='partial' and not (root/'actual-early-boundary.json').exists():
  count,full=early_partial(stream,data,offset)
  actual_mixed_marker(root/'actual-early-boundary.json',{'boundary':'partial','actual_partial_bytes':count,'actual_full_bytes':full})
  threading.Event().wait()
 mode=root/'recovery-boundary';marker=root/'actual-early-recovery-boundary.json'
 if (mode.exists() and mode.read_text() in {'cancel','owner_exit'} and actual_target_attempt is not None
     and actual_target_attempt in Path(stream.name).parts and not marker.exists()):
  count,full=early_partial(stream,data,offset)
  actual_mixed_marker(marker,{'boundary':mode.read_text(),'actual_partial_bytes':count,'actual_full_bytes':full})
  actual_early_recovery_release.wait()
  offset+=count
 return original(stream,data,offset)
def actual_early_append(stream,data,offset):
 return actual_early_append_source(stream,data,offset,original_early_append)
def actual_early_append_log(stream,data,offset):
 return actual_early_append_source(stream,data,offset,original_early_append_log)
early_stages.append_bytes=actual_early_append
early_stages.append_log=actual_early_append_log
'''


def verify(boundary,*,handler='report',s3=False,database_url=None,source_profile_id=None,recovery_boundary=None,stage_fault=None,closed_archive_peers=False):
 from app.services import process_identity_native as identity
 from app.services.windows_process_scope import ProcessHandle
 api=IsolatedServer('early-finalizing',app='output_fixture:app',database_url=database_url)
 store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') if s3 else None
 report={'passed':False,'boundary':boundary,'handler':handler,'backend':'s3' if s3 else 'fs','database':'postgresql' if database_url else 'sqlite','recovery_boundary':recovery_boundary,
  'stage_fault':stage_fault,'closed_archive_peers':closed_archive_peers,'model_calls':0,'mock_llm_calls':0,'developer_database_modified':False,'API_births':[],'API_exit_witnesses':[],'checks':[]}
 handles={};peer_tokens={};recovery_socket=None
 configuration=None;nonce=None;live_fixture=''
 if source_profile_id is not None:
  from live_llm_acceptance import source,FIXTURE as actual_live_fixture
  configuration=asyncio.run(source(source_profile_id));nonce='RECOVERED_'+uuid.uuid4().hex;live_fixture=actual_live_fixture
 def save():
  names=set(SOURCES)|{'early_finalizing_acceptance.py','attempt_professional_finalizing_acceptance.py','app/services/finalizing_stage_recovery.py','cae_fixtures.py'}
  if configuration is not None:
   names.update({'live_llm_early_finalizing_acceptance.py','live_llm_acceptance.py','app/services/llm.py'})
   trace=api.root/'upstream.jsonl'
   if trace.exists():report['model_calls']=sum(v.get('kind')=='http' and v.get('path','').endswith('/chat/completions') for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
  report['source_hashes']={n:hashlib.sha256((SERVER/n).read_bytes()).hexdigest() for n in sorted(names)}
  raw=json.dumps(report,indent=2)+'\n'
  if configuration is not None:raw=raw.replace(configuration['key'],'[REDACTED]')
  (api.root/'early-result.json').write_text(raw,encoding='utf8')
 def call(method,path,body=None,expect=200,*,base=None):
  with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+peer_tokens.get(base or api.base,api.token)},trust_env=False,timeout=180) as client:
   response=client.request(method,path,json=body)
   try:value=response.json()
   except ValueError:value={'status':response.status_code}
   assert response.status_code==expect,(method,path,response.status_code,value)
   return value
 def remember(*,base=None):
  birth=call('GET','/test/output-process',base=base);handles[json.dumps(birth,sort_keys=True)]=ProcessHandle.query(birth)
  report['API_births'].append(birth);save();return birth
 def exited(birth):
  key=json.dumps(birth,sort_keys=True);handle=handles[key];handle.wait_exit(10);assert handle.identity(allow_exited=True)==birth
  if not any(v['birth']==birth for v in report['API_exit_witnesses']):
   report['API_exit_witnesses'].append({'birth':birth,'observation':{'state':'exited','reason':'retained_creation_handle'},'observer':identity.observe(os.getpid()),'observed_at_unix':time.time()});save()
 def restart():
  birth=call('GET','/test/output-process')
  try:call('POST','/test/output-exit')
  except httpx.TransportError:pass
  else:raise AssertionError('Actual exit returned HTTP')
  exited(birth);api.__exit__(RuntimeError,None,None)
  api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token'];remember()
 @contextlib.contextmanager
 def peer():
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  assert port!=8000;base='http://127.0.0.1:'+str(port);report.setdefault('peer_apis',[]).append(base);save()
  with (api.root/('peer-'+str(port)+'.log')).open('w',encoding='utf8') as log:
   proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','output_fixture:app','--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   birth=None
   try:
    until=time.monotonic()+30
    with httpx.Client(trust_env=False,timeout=1) as client:
     while True:
      assert proc.poll() is None,'Actual peer API launcher exited'
      try:
       if client.get(base+'/api/health').json().get('ok'):break
      except (httpx.HTTPError,ValueError):pass
      assert time.monotonic()<until,'Actual peer API startup timed out';time.sleep(.1)
    peer_tokens[base]=api.token;birth=remember(base=base);yield base
   finally:
    try:
     if birth is not None:
      try:call('POST','/test/output-exit',base=base)
      except httpx.TransportError:pass
      exited(birth)
    finally:
     peer_tokens.pop(base,None)
     if proc.poll() is None:proc.terminate()
     try:proc.wait(timeout=10)
     except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
 def open_request(path,body):
  from urllib.parse import urlsplit
  target=urlsplit(api.base);data=json.dumps(body).encode();selected=socket.create_connection((target.hostname,target.port),timeout=10)
  headers=('POST /api/v1'+path+' HTTP/1.1\r\nHost: '+target.netloc+'\r\nAuthorization: Bearer '+api.token+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(data))+'\r\n\r\n').encode()
  selected.sendall(headers+data);return selected
 print('EARLY_FINALIZING '+str(api.root)+(' STORE '+str(store.root) if store else ''),flush=True)
 try:
  with store if store else contextlib.nullcontext():
   (api.root/'normal-mode').write_text('Original upload proceeds only after early-stage recovery\n',encoding='utf8')
   (api.root/'native-handler').write_text(handler,encoding='utf8');(api.root/'early-boundary').write_text(boundary,encoding='utf8')
   if recovery_boundary:(api.root/'recovery-boundary').write_text(recovery_boundary,encoding='utf8')
   if stage_fault=='expired':(api.root/'budget-recovery-mode').write_text('expired',encoding='utf8')
   if closed_archive_peers:(api.root/'closed-archive-peers').write_text('Actual archive materialized before metadata; no new copy on peers\n',encoding='utf8')
   settings='import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
   if stage_fault=='disk_limit':settings+='import os\nos.environ["VIBEFLOW_EXECUTION_WORKSPACE_LIMIT_MB"]="1"\n'
   (api.root/'output_fixture.py').write_text(settings+FIXTURE+'\n'+EXTRA+'\n'+EARLY+'\n'+live_fixture,encoding='utf8')
   with api:
    api.create_case('Actual early finalization recovery');user=api.call('GET','/auth/me');remember()
    if handler=='report':
     api.put('E-report/report.md','# Original recovery document\n\nActual Pandoc early-stage recovery.\n'+('\n'+nonce+'\n' if nonce else ''))
     submitted=call('POST',api.prefix+'/report:build',{})
    elif handler=='latex':
     api.put('E-report/latex/main.tex',r'\documentclass{article}'+'\n'+r'\begin{document}Original recovery document.\end{document}'+'\n')
     submitted=call('POST',api.prefix+'/latex:build',{'recipe':'pdflatex'})
    else:
     from cae_fixtures import cavity_2d
     for path,content in cavity_2d().items():api.put(path,content)
     command='source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && '
     mesh=call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl','command':command+'blockMesh','timeout_seconds':120})
     api.wait_job(mesh['id']);report['actual_mesh_job_id']=mesh['id']
     submitted=call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':'C-run/case-1','command':command+'icoFoam','timeout_seconds':120})
    job_id=submitted.get('job_id',submitted.get('id'));report['job_id']=job_id;save()
    until=time.monotonic()+150;marker=api.root/'actual-early-boundary.json'
    while not marker.exists():
     state=call('GET','/test/output-state/'+str(job_id))
     assert state['job_status'] not in {'failed','cancelled','interrupted','succeeded'},state['job_status']
     assert time.monotonic()<until,'Actual early stage was not reached';time.sleep(.05)
    before=call('GET','/test/output-state/'+str(job_id));report['before_exit']=before;report['actual_boundary']=json.loads(marker.read_text());save()
    assert before['phase']=='finalizing' and before['write_checkpoint']=={} and 'output_preparation' not in before['output']
    stage=call('GET','/jobs/'+str(job_id)+'/preparation-recovery');assert stage['state']=='preparation_pending'
    body={'attempt_id':before['attempt_id'],'stage_sha256':stage['stage_sha256']}
    refused=call('POST','/jobs/'+str(job_id)+':recover-preparation',body,409);assert refused['detail']['code']=='recovery_required'
    report['live_original_owner_refusal']=refused;save();restart()
    preparation_path='/jobs/'+str(job_id)+':recover-preparation'
    if stage_fault=='expired':
     assert before['input']['execution_spec']['timeout_seconds']==60
     while time.time()<=before['original_deadline_unix']+.05:time.sleep(.25)
     report['actual_deadline_elapsed']={'deadline':before['original_deadline_unix'],'observed_at':time.time()};save()
    if stage_fault in {'disk_limit','cache_missing'}:
     case_roots=list((api.root/'files/workspaces').glob('*/cases/'+str(api.case_id)));assert len(case_roots)==1
     case_root=case_roots[0].resolve();run=(case_root/before['input']['workspace']['run_path']).resolve()
     assert case_root.is_relative_to(api.root.resolve()) and run.is_relative_to(case_root) and run.name=='run'
     if stage_fault=='disk_limit':
      assert before['input']['storage_policy']['max_workspace_bytes']==1024*1024
      growth=run.parent/'actual-recovery-node-growth.bin';assert not growth.exists() and growth.resolve().is_relative_to(api.root.resolve())
      with growth.open('xb') as stream:
       for _ in range(32):stream.write(b'R'*65536)
       stream.flush();os.fsync(stream.fileno())
      report['actual_node_growth']={'bytes':growth.stat().st_size,'sha256':hashlib.sha256(growth.read_bytes()).hexdigest(),'path':growth.relative_to(api.root).as_posix()};save()
      assert report['actual_node_growth']['bytes']==2*1024*1024
     else:
      held=run.parent/'actual-retained-preproof-run';assert not held.exists() and held.resolve().is_relative_to(api.root.resolve()) and held.parent==run.parent
      def retained_files(directory):
       found={}
       for path in directory.rglob('*'):
        assert not path.is_symlink()
        if path.is_file():found[path.relative_to(directory).as_posix()]={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
       return found
      original_files=retained_files(run);assert original_files=={name:{key:record[key] for key in ('size','sha256')} for name,record in before['output']['finalizing_stage']['files'].items()}
      run.rename(held);assert not run.exists() and retained_files(held)==original_files
      refusal=call('POST',preparation_path,body,409);assert refusal['detail']['code']=='early_finalization_unavailable'
      missing=call('GET','/test/output-state/'+str(job_id));assert missing['phase']=='finalizing' and missing['write_checkpoint']=={} and not missing['finalization_requests'] and 'output_preparation' not in missing['output']
      assert missing['input']==before['input'] and missing['attempt_id']==before['attempt_id'] and missing['original_deadline_unix']==before['original_deadline_unix']
      plan=call('GET','/jobs/'+str(job_id)+'/recovery-disposition')
      disposition=call('POST','/jobs/'+str(job_id)+':retain-for-review',{'attempt_id':plan['attempt_id'],'snapshot_sha256':plan['snapshot_sha256'],'action':'retain_for_review'})
      assert not disposition['execution_authorized'] and not disposition['content_drained']
      assert retained_files(held)==original_files and not run.exists()
      report['actual_missing_cache']={'refusal':refusal,'state':missing,'retained_files':original_files,'manual_disposition':disposition,'no_input_recompute':True};save()
      held.rename(run);assert not held.exists() and retained_files(run)==original_files
      report['actual_original_cache_restored']=True;save()
    if recovery_boundary:
     with peer() as peer_base:
      recovery_socket=open_request(preparation_path,body)
      marker=api.root/'actual-early-recovery-boundary.json';until=time.monotonic()+60
      while not marker.exists():
       assert time.monotonic()<until,'Actual independent recovery did not reach boundary';time.sleep(.05)
      report['actual_recovery_boundary']=json.loads(marker.read_text());save()
      refused=call('POST',preparation_path,body,409,base=peer_base);assert refused['detail']['code']=='early_finalization_busy'
      report['live_recovery_peer_refusal']=refused
      held=call('GET','/test/output-state/'+str(job_id));report['recovery_inflight']=held
      report['recovery_evidence_inflight']=call('GET','/test/early-recovery-evidence/'+str(job_id));save()
      assert call('GET','/test/early-recovery-task/'+str(job_id))['available_request_slots']==1
      assert any(r['state']=='active' for r in report['recovery_evidence_inflight']['operations']) and not held['finalization_requests']
      if recovery_boundary=='cancel':
       recovery_socket.shutdown(socket.SHUT_RDWR);recovery_socket.close();recovery_socket=None
       until=time.monotonic()+10
       while not call('GET','/test/early-recovery-task/'+str(job_id))['cancelling']:
        assert time.monotonic()<until,'Actual disconnected recovery was not cancelled';time.sleep(.02)
       assert not call('GET','/test/early-recovery-task/'+str(job_id))['done']
       report['cancelled_but_blocked']=call('GET','/test/early-recovery-evidence/'+str(job_id));assert any(r['state']=='active' for r in report['cancelled_but_blocked']['operations'])
       refused=call('POST',preparation_path,body,409,base=peer_base);assert refused['detail']['code']=='early_finalization_busy'
       report['actual_recovery_cancel_release']=call('POST','/test/early-recovery-release/'+str(job_id));assert report['actual_recovery_cancel_release']['actual_thread_joined']
      else:
       if recovery_boundary=='capture_commit':
        full_body={'attempt_id':before['attempt_id'],'inventory_sha256':held['output']['output_preparation']['inventory_sha256']}
        full_refused=call('POST','/jobs/'+str(job_id)+':finalize-outputs',full_body,409,base=peer_base)
        assert full_refused['detail']['code'] in {'recovery_required','file_recovery_required','early_finalization_busy','output_recovery_busy'};report['held_capture_full_refusal']=full_refused
       restart()
       recovery_socket.close();recovery_socket=None
      captured=call('POST',preparation_path,body,base=peer_base)
      assert call('POST',preparation_path,body)==captured
      evidence=call('GET','/test/early-recovery-evidence/'+str(job_id));report['recovery_evidence_after']=evidence;save()
      assert len(evidence['operations'])==2 and all(r['state']=='settled' for r in evidence['operations'])
      exits=[e for e in evidence['events'] if e['type']=='attempt.finalizing_stage_io_exited'];returns=[e for e in evidence['events'] if e['type']=='attempt.finalizing_stage_io_drained']
      assert len(exits)==(0 if recovery_boundary=='cancel' else 1) and len(returns)==(2 if recovery_boundary=='cancel' else 1)
      captures=[e for e in evidence['events'] if e['type']=='attempt.finalizing_stage_recovered'];assert len(captures)==1
      assert call('GET','/test/early-recovery-task/'+str(job_id))['available_request_slots']==2
      assert call('GET','/test/early-recovery-task/'+str(job_id),base=peer_base)['available_request_slots']==2
      if recovery_boundary=='capture_commit':assert captured['inventory_sha256']==report['actual_recovery_boundary']['response']['inventory_sha256']
    else:captured=call('POST',preparation_path,body)
    report['captured']=captured;save()
    assert captured['state']=='captured' and not captured['native_execution_started'] and not captured['solver_execution_started']
    assert call('POST','/jobs/'+str(job_id)+':recover-preparation',body)==captured
    prepared=call('GET','/test/output-state/'+str(job_id));report['prepared_state']=prepared;save()
    assert prepared['input']==before['input'] and prepared['attempt_id']==before['attempt_id'] and prepared['original_deadline_unix']==before['original_deadline_unix']
    assert prepared['output']['output_preparation']['schema']=='vibeflow.attempt-output-preparation.v3'
    assert prepared['output']['finalizing_source']['schema']=='vibeflow.finalizing-source.v2' and prepared['output']['finalizing_source']['pre_capture_stages_returned'] is False
    assert prepared['output']['finalizing_stage']==before['output']['finalizing_stage'] and prepared['write_checkpoint']=={} and not prepared['finalization_requests']
    final_body={'attempt_id':before['attempt_id'],'inventory_sha256':captured['inventory_sha256']}
    if closed_archive_peers:
     archive_path='/jobs/'+str(job_id)+':recover-outputs'
     call('POST','/test/output-arm-commit',{'stage':'before'})
     recovery_socket=open_request(archive_path,final_body)
     marker=api.root/'actual-output-metadata-boundary.json';until=time.monotonic()+90
     while not marker.exists():
      assert time.monotonic()<until,'Actual closed archive metadata boundary was not reached';time.sleep(.05)
     materialized=call('GET','/test/output-state/'+str(job_id));report['closed_archive_before_metadata']=materialized
     report['actual_archive_metadata_boundary']=json.loads(marker.read_text());save()
     assert materialized['output']['object_store']['state']=='pending'
     assert materialized['reservation']['state']=='materialized' and materialized['reservation']['remaining_bytes']==0
     assert not [e for e in materialized['copy_events'] if e['type']=='attempt.outputs_recovered']
     restart();recovery_socket.close();recovery_socket=None
     rolled_back=call('GET','/test/output-state/'+str(job_id));report['closed_archive_after_owner_exit']=rolled_back;save()
     assert rolled_back['output']['object_store']['state']=='pending' and rolled_back['reservation']['state']=='materialized'
     assert not [e for e in rolled_back['copy_events'] if e['id']==report['actual_archive_metadata_boundary']['recovery_event_id']]
     with peer() as peer_base:
      with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
       futures=[pool.submit(call,'POST',archive_path,final_body,base=base) for base in (api.base,peer_base)]
       replies=[future.result() for future in futures]
      assert replies[0]==replies[1] and replies[0]['state']=='archived'
      archived=call('GET','/test/output-state/'+str(job_id));events=[e for e in archived['copy_events'] if e['type']=='attempt.outputs_recovered']
      assert len(events)==1 and events[0]['id']==replies[0]['recovery_event_id']
      assert events[0]['id']!=report['actual_archive_metadata_boundary']['recovery_event_id']
      assert archived['reservation']['state']=='materialized' and archived['reservation']['remaining_bytes']==0
      copies=[json.loads(line) for line in (api.root/'actual-archive-copy-invocations.jsonl').read_text().splitlines()]
      assert len(copies)==1 and copies[0]['actual_resume_objects_called']
      report['actual_closed_archive_peer_replies']=replies;report['closed_archive_after_peers']=archived;report['actual_archive_copy_invocations']=copies;save()
    elif s3:assert call('POST','/jobs/'+str(job_id)+':recover-outputs',final_body)['state']=='archived'
    else:assert call('POST','/jobs/'+str(job_id)+':restore-output-cache',final_body)['state']=='cache_ready'
    final=call('POST','/jobs/'+str(job_id)+':finalize-outputs',final_body);report['finalization']=final;save()
    resource_failure=stage_fault in {'expired','disk_limit'}
    assert final['state']==('failed' if resource_failure else 'succeeded') and not final['solver_execution_started'] and not final['finalization_pending']
    assert call('POST','/jobs/'+str(job_id)+':finalize-outputs',final_body)==final
    state=call('GET','/test/output-state/'+str(job_id));report['final_state']=state;save()
    assert state['phase']==state['job_status']==final['state'] and state['input']==before['input'] and state['original_deadline_unix']==before['original_deadline_unix']
    assert state['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5' and state['write_checkpoint']['stop']==before['actual_in_memory_stop']
    assert state['publication_state']['id']==before['publication_state']['id']
    assert state['publication_state']['current_id']==before['publication_state']['current_id' if resource_failure else 'id']
    if resource_failure:
     assert state['reason_code']=='resource_limit' and not final['native_execution_started'] and not final['native_validation_started']
     assert len(state['finalization_requests'])==1
     assert state['finalization_requests'][0]['children'] in ({},{'schema':'vibeflow.native-request-children.v1','children':{}})
     assert state['finalization_requests'][0]['result']['usage']['violation']==('timeout' if stage_fault=='expired' else 'disk_limit')
     original_usage=before['output']['finalizing_stage']['usage'];captured_usage=prepared['output']['output_preparation']['output_usage']
     assert all(captured_usage[key]==value for key,value in original_usage.items() if key not in {'violation','violation_source'})
     report['checks'].append({'check':'actual_original_resource_limit_after_preproof_capture_without_validator_or_solver','mode':stage_fault})
    releases=[json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]
    assert sum(v['job_id']==job_id for v in releases)==1;report['actual_original_root_releases']=releases
    if resource_failure:pass
    elif handler=='report':
     html=api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True);assert b'Original recovery document' in html
    elif handler=='latex':
     from app.services.latex_artifacts import paths
     pdf=api.call('GET',api.prefix+'/raw?path='+paths('E-report/latex/main.tex')['pdf'],raw=True);assert pdf.startswith(b'%PDF-')
    else:
     field=api.call('GET',api.prefix+'/raw?path=C-run/case-1/0.5/U',raw=True);assert len(field)>10000
     evidence=json.loads(api.call('GET',api.prefix+'/raw?path=.vibeflow/jobs/job-'+str(job_id)+'-evidence.json',raw=True))
     assert evidence['summary']['finished_cleanly'] and evidence['job_id']==job_id
     report['actual_recovered_solve_evidence']=evidence
    report['checks'].append({'check':'actual_early_stage_same_attempt_resource_failure' if resource_failure else 'actual_early_stage_same_attempt_capture_archive_validation_and_original_publication','backend':report['backend'],'boundary':boundary,'handler':handler})
    if configuration is not None:
     profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual early finalization readback','base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
     conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Read actual early recovered source','provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'plan'})
     events=[]
     with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
      with client.stream('POST','/conversations/'+str(conversation['id'])+'/messages',json={'content':'请仅调用一次 read_text_file 工具读取 E-report/report.md。拿到实际内容后，只原样返回其中以 RECOVERED_ 开头的完整验证字符串。不要猜测，不要修改文件，不要调用其他工具。','provider_profile_id':profile['id'],'model':configuration['model']}) as response:
       response.raise_for_status()
       for line in response.iter_lines():
        if line.startswith('data:'):events.append(json.loads(line[5:]))
     upstream=[json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
     calls=[v for v in upstream if v.get('kind')=='http' and v.get('path','').endswith('/chat/completions')];usage=[v for v in upstream if v.get('usage')]
     report['actual_model_readback']={'host':'api.deepseek.com','model':configuration['model'],'HTTP_statuses':[v['status'] for v in calls],'usage_receipts':len(usage),'reported_tokens':sum(v['usage']['total_tokens'] for v in usage),'nonce':nonce,'answer':''.join(e.get('text','') for e in events if e.get('type')=='text.delta'),'upstream':upstream}
     raw=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
     (api.root/'actual-early-model-events.json').write_text(raw+'\n',encoding='utf8');save()
     assert len(calls)==len(usage)==2 and all(v['status']==200 and v['host']=='api.deepseek.com' for v in calls)
     assert not [e for e in events if e.get('type') in {'stream.error','request.failed','request.timed_out'}]
     results=[e for e in events if e.get('type')=='tool.result'];assert len(results)==1 and results[0]['tool']=='read_text_file' and results[0]['ok']
     assert nonce in report['actual_model_readback']['answer'] and any(e.get('type')=='run.completed' for e in events)
     report['checks'].append({'check':'actual_DeepSeek_reads_early_recovered_source_through_real_file_tool','model_calls':2,'mock_llm_calls':0})
    report['functional_passed']=True
 except BaseException as error:
  report['failure']={'type':type(error).__name__,'detail':str(error)[:1000],'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
 finally:
  if recovery_socket is not None:recovery_socket.close()
  try:
   current=call('GET','/test/output-process')
   if current in report['API_births']:
    try:call('POST','/test/output-exit')
    except httpx.TransportError:pass
  except (httpx.HTTPError,AssertionError):pass
  report['API_exit_probes']=[]
  for birth in report['API_births']:
   try:exited(birth);probe={'state':'exited','reason':'retained_creation_handle'}
   except BaseException as error:probe=identity.probe(birth);report.setdefault('API_exit_probe_failures',[]).append(type(error).__name__)
   report['API_exit_probes'].append(probe)
  for handle in handles.values():handle.close()
  report['store_stopped']=store.proc is not None and store.proc.poll() is not None if store else None
  report['passed']=bool(report.get('functional_passed') and not report.get('API_exit_probe_failures') and all(v['state']=='exited' for v in report['API_exit_probes']) and (not store or report['store_stopped']));save()
 print('EARLY_FINALIZING_RESULT '+str(api.root/'early-result.json'),flush=True)
 return report['passed'],api.root


async def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--boundary',choices=['before','partial'],required=True);parser.add_argument('--handler',choices=['report','latex','solve'],default='report');parser.add_argument('--s3',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--source-profile-id',type=int);parser.add_argument('--recovery-boundary',choices=['cancel','owner_exit','capture_commit']);parser.add_argument('--stage-fault',choices=['expired','disk_limit','cache_missing']);parser.add_argument('--closed-archive-peers',action='store_true');args=parser.parse_args(argv)
 if args.recovery_boundary and args.boundary!='before':parser.error('Independent recovery lifecycle requires original before-stage pause')
 if args.stage_fault and (args.boundary!='before' or args.handler!='report' or args.recovery_boundary or args.source_profile_id is not None):parser.error('Original resource/cache fault uses the real report before-stage entry without recovery-boundary or model inference')
 if args.closed_archive_peers and (not args.s3 or args.boundary!='before' or args.handler!='report' or args.recovery_boundary or args.stage_fault or args.source_profile_id is not None):parser.error('Closed archive concurrency uses the actual report before-stage S3 entry without other fault modes or inference')
 if args.source_profile_id is not None and (__name__=='__main__' or args.handler!='report'):parser.error('Use explicit live_llm_early_finalizing_acceptance.py for actual model acceptance')
 async def run(url=None):return await asyncio.to_thread(verify,args.boundary,handler=args.handler,s3=args.s3,database_url=url,source_profile_id=args.source_profile_id,recovery_boundary=args.recovery_boundary,stage_fault=args.stage_fault,closed_archive_peers=args.closed_archive_peers)
 if not args.postgres:return 0 if (await run())[0] else 1
 import asyncpg,re
 from sqlalchemy.engine import make_url
 from app.config import Settings
 from workflow_migration_smoke import alembic
 selected=make_url(Settings(_env_file=SERVER/'.env').database_url);assert selected.drivername=='postgresql+asyncpg' and selected.host in {'localhost','127.0.0.1','::1'}
 name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
 root=Path(tempfile.mkdtemp(prefix='early-finalizing-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
 print('EARLY_FINALIZING_POSTGRES '+str(root),flush=True)
 connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
 try:
  await connection.execute('CREATE DATABASE "'+name+'"');created=True;url=selected.set(database=name).render_as_string(hide_password=False)
  await asyncio.to_thread(alembic,url,root,'upgrade','head');passed,api=await run(url);report.update(passed=passed,api_root=str(api));return 0 if passed else 1
 finally:
  try:
   if created:await connection.execute('DROP DATABASE "'+name+'" WITH (FORCE)');report['temporary_database_dropped']=True
  finally:
   await connection.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
if __name__=='__main__':raise SystemExit(asyncio.run(main()))
