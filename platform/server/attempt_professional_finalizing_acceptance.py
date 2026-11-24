"""Actual registered document compilation, interrupted S3 archive and full commit."""
import argparse,asyncio,concurrent.futures,contextlib,hashlib,io,json,os,re,shutil,socket,subprocess,sys,tempfile,time,traceback,uuid,zipfile
from pathlib import Path
import httpx
from testsupport import SERVER,IsolatedServer
from s3testsupport import ObjectStore
from attempt_output_recovery_acceptance import FIXTURE,SOURCES

EXTRA=r'''
from app.services.report_handler import ReportHandler as actual_ReportHandler
original_report_prepare=actual_ReportHandler.prepare
async def actual_report_prepare(self,*args,**kwargs):
 marker=root/'budget-recovery-mode'
 if marker.exists():
  mode=marker.read_text()
  kwargs['budget']={**kwargs['budget'],'timeout_seconds':60 if mode=='expired' else 300,
   'max_log_bytes':32768 if mode=='log_limit' else 16*1024*1024}
 return await original_report_prepare(self,*args,**kwargs)
actual_ReportHandler.prepare=actual_report_prepare
from app.services import output_archive_storage as actual_archive_storage
original_archive_admission=actual_archive_storage.admit
async def actual_archive_admission(*args,**kwargs):
 if (root/'pause-original-archive-admission').exists() and not (root/'actual-archive-admission-boundary.json').exists():
  marker=root/'actual-archive-admission-boundary.json'
  with marker.open('x',encoding='utf8') as stream:
   json.dump({'actual_original_preparation_returned':True,'archive_admitted':False},stream);stream.flush();os.fsync(stream.fileno())
  await asyncio.Event().wait()
 return await original_archive_admission(*args,**kwargs)
actual_archive_storage.admit=actual_archive_admission
from app.services.native_execution import NativeExecutor
from sqlalchemy import event as actual_sql_event
from app.db import engine as actual_engine
import time as actual_time,traceback as actual_traceback
actual_sql_transactions={}
original_cancel_recover=full_recovery.recover
actual_HTTP_recovery_tasks={}
@app.get('/api/v1/test/output-cancellation-observed/{identifier}')
async def actual_cancellation_observed(identifier:int,user=Depends(get_current_user)):
 task=actual_HTTP_recovery_tasks.get(identifier)
 return {'actual_operation_cancelled':bool(task and (task.cancelling() or task.cancelled()))}
async def actual_cancel_recover(*args,**kwargs):
 started=actual_time.monotonic();running=asyncio.current_task()
 actual_HTTP_recovery_tasks[args[0]]=running
 async def diagnose():
  while True:
   await asyncio.sleep(5)
   frames=[];current=running.get_coro()
   while current is not None and len(frames)<50:
    frame=getattr(current,'cr_frame',None) or getattr(current,'gi_frame',None)
    if frame is not None:frames.append({'file':Path(frame.f_code.co_filename).name,'line':frame.f_lineno,'function':frame.f_code.co_name})
    current=getattr(current,'cr_await',None) or getattr(current,'gi_yieldfrom',None)
   (root/'actual-finalization-wait-frames.json').write_text(json.dumps({'elapsed':actual_time.monotonic()-started,'frames':frames}),encoding='utf8')
 watcher=asyncio.create_task(diagnose())
 try:return await original_cancel_recover(*args,**kwargs)
 finally:
  watcher.cancel();await finish_before_cancel(asyncio.gather(watcher,return_exceptions=True))
  with (root/'actual-finalization-durations.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps({'elapsed':actual_time.monotonic()-started})+'\n')
full_recovery.recover=actual_cancel_recover
def actual_sql_trace(kind,connection=None,extra=None):
 if not (root/'trace-native-transactions').exists():return
 key=id(connection) if connection is not None else (extra or {}).get('connection')
 value={'kind':kind,'connection':key,'pid':os.getpid(),'at':actual_time.monotonic(),**(extra or {})}
 if kind=='begin':actual_sql_transactions[key]={'started_at':value['at'],'last_statement':None,'pending':False}
 elif kind=='checked_in':actual_sql_transactions.pop(key,None)
 elif kind in {'commit','rollback'} and key in actual_sql_transactions:actual_sql_transactions[key]['ending']=kind
 elif kind=='statement' and key in actual_sql_transactions:actual_sql_transactions[key].update(last_statement=value.get('statement'),pending=True)
 elif kind=='returned' and key in actual_sql_transactions:actual_sql_transactions[key]['pending']=False
 value['open_transactions']={str(k):v for k,v in actual_sql_transactions.items()}
 with (root/'actual-sql-transaction-trace.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\n')
@actual_sql_event.listens_for(actual_engine.sync_engine,'begin')
def actual_sql_begin(connection):
 connection.info['actual_trace_connection_id']=id(connection)
 actual_sql_trace('begin',connection)
@actual_sql_event.listens_for(actual_engine.sync_engine.pool,'checkin')
def actual_sql_checkin(connection,record):actual_sql_trace('checked_in',extra={'connection':record.info.get('actual_trace_connection_id')})
@actual_sql_event.listens_for(actual_engine.sync_engine,'commit')
def actual_sql_commit(connection):actual_sql_trace('commit',connection)
@actual_sql_event.listens_for(actual_engine.sync_engine,'rollback')
def actual_sql_rollback(connection):actual_sql_trace('rollback',connection)
@actual_sql_event.listens_for(actual_engine.sync_engine,'before_cursor_execute')
def actual_sql_statement(connection,cursor,statement,parameters,context,executemany):
 if not (root/'trace-native-transactions').exists():return
 if statement.lstrip().split(None,1)[0].upper() not in {'UPDATE','INSERT','DELETE'}:return
 names=[n for n in ['native_requests','case_event_counters','durable_events','event_outbox','jobs','job_attempts'] if n in statement]
 actual_sql_trace('statement',connection,{'statement':{'kind':statement.lstrip().split(None,1)[0].upper(),'tables':names},
  'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in actual_traceback.extract_stack() if '/app/' in f.filename.replace('\\','/')][-6:]})
@actual_sql_event.listens_for(actual_engine.sync_engine,'after_cursor_execute')
def actual_sql_returned(connection,*args):actual_sql_trace('returned',connection)
@actual_sql_event.listens_for(actual_engine.sync_engine,'handle_error')
def actual_sql_error(context):
 failure=context.original_exception
 actual_sql_trace('error',context.connection,{'exception':type(failure).__name__,
  'sqlite_errorcode':getattr(failure,'sqlite_errorcode',None),'sqlite_errorname':getattr(failure,'sqlite_errorname',None)})
original_native_execute=NativeExecutor._execute
async def actual_native_execute(self,entry,purpose,*args,**kwargs):
 try:return await original_native_execute(self,entry,purpose,*args,**kwargs)
 except BaseException as error:
  import traceback
  value={'kind':self.kind,'purpose':purpose,'type':type(error).__name__,
   'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
  with (root/'actual-native-error-frames.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\n')
  raise
NativeExecutor._execute=actual_native_execute
original_native_command=NativeExecutor._command
def actual_native_command(self,purpose,parameters):
 argv,timeout,limit=original_native_command(self,purpose,parameters)
 marker=root/'actual-validation-running.json'
 if hasattr(self,'recovery_profile') and (root/'interrupt-validation').exists() and not marker.exists() and purpose in {'pandoc_compile','latex_compile'}:
  from app.services.owned_process import wsl_path
  probe=Path(identity.__file__).resolve();wrapper=root/'actual-validation-pause.py'
  if self.runtime=='wsl':
   assert argv[3:9]==['--user','nobody','--exec','python3','-I','-S']
   argv=[*argv[:9],wsl_path(wrapper),wsl_path(marker),wsl_path(probe),'--',*argv[9:]]
  else:
   assert argv[1:4]==['-I','-S','-u']
   argv=[*argv[:4],str(wrapper),str(marker),str(probe),'--',*argv[4:]]
 return argv,timeout,limit
NativeExecutor._command=actual_native_command
from app.services import native_request_records as actual_requests,finalizing_native_scope as actual_scopes
from app.services import finalizing_native_usage as actual_usage
original_actual_usage=actual_usage.checkpoint
async def actual_usage_checkpoint(*args,**kwargs):
 try:
  value=await original_actual_usage(*args,**kwargs)
  selected=root/'budget-recovery-mode';compiled=root/'actual-validation-running.json';marker=root/'actual-budget-diagnostic-boundary.json'
  if selected.exists() and compiled.exists() and not marker.exists() and not kwargs.get('sealed'):
   executor,entry,diagnostics=args[:3]
   if diagnostics is not None:
    # Keep subsequent durable samples behind the actual usage lock. The
    # original compiler already returned; grow its real diagnostic inode after
    # the committed sample, then terminate the real API at this boundary.
    async with executor.usage_lock:
     if not marker.exists():
      path=diagnostics.folder/'stderr.bin';before=path.stat().st_size
      amount=32768 if selected.read_text()=='log_limit' else 8192
      with path.open('ab') as stream:stream.write(os.urandom(amount));stream.flush();os.fsync(stream.fileno())
      actual_mixed_marker(marker,{**json.loads(compiled.read_text()),'diagnostic_relative':diagnostics.relative,
       'diagnostic_before':before,'diagnostic_after':path.stat().st_size,'actual_diagnostic_growth':amount})
      await asyncio.Event().wait()
  return value
 except BaseException as error:
  value={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in actual_traceback.extract_tb(error.__traceback__)]}
  with (root/'actual-usage-checkpoint-errors.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\n')
  raise
actual_usage.checkpoint=actual_usage_checkpoint
from app.services.native_request_execution import RequestExecutor as actual_RequestExecutor
def actual_mixed_marker(path,value):
 pending=path.with_suffix('.pending')
 with pending.open('x',encoding='utf8') as stream:json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
 os.replace(pending,path)
original_mixed_finalizer=actual_RequestExecutor.run_finalizer
async def actual_mixed_finalizer(self,*args,**kwargs):
 value=await original_mixed_finalizer(self,*args,**kwargs)
 marker=root/'actual-mixed-error.json'
 if (root/'mixed-error-after-finalizer').exists() and not marker.exists():
  try:
   with (root/'actual-missing-required-sidecar').open('rb'):pass
  except FileNotFoundError:
   actual_mixed_marker(marker,{'request_id':self.owner.id,'error':'FileNotFoundError','actual_native_finalizer_returned':True})
   raise
 return value
actual_RequestExecutor.run_finalizer=actual_mixed_finalizer
original_mixed_close=actual_RequestExecutor.close
async def actual_mixed_close(self):
 marker=root/'actual-mixed-error.json';ready=root/'actual-mixed-close.json'
 if hasattr(self,'recovery_profile') and marker.exists() and not ready.exists():
  value=json.loads(marker.read_text())
  if value['request_id']==self.owner.id:
   actual_mixed_marker(ready,dict(value,actual_close_entered=True));until=actual_time.monotonic()+30
   while not (root/'release-preparation').exists():
    if actual_time.monotonic()>=until:raise TimeoutError('Actual failed scope cleanup was not released')
    await asyncio.sleep(.02)
 return await original_mixed_close(self)
actual_RequestExecutor.close=actual_mixed_close
original_mixed_complete=actual_requests.complete
async def actual_mixed_complete(owner,*args,**kwargs):
 try:return await original_mixed_complete(owner,*args,**kwargs)
 except BaseException as error:
  marker=root/'actual-mixed-error.json'
  if marker.exists() and json.loads(marker.read_text())['request_id']==owner.id:
   actual_mixed_marker(root/'actual-mixed-completion-rejection.json',{'request_id':owner.id,'error':type(error).__name__})
  raise
actual_requests.complete=actual_mixed_complete
original_actual_heartbeat=actual_requests.heartbeat
async def actual_heartbeat(owner):
 try:return await original_actual_heartbeat(owner)
 except BaseException as failure:
  if not isinstance(failure,asyncio.CancelledError):
   value={'type':type(failure).__name__,'code':getattr(failure,'code',None),
    'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in actual_traceback.extract_tb(failure.__traceback__)]}
   with (root/'actual-heartbeat-error-frames.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value)+'\n')
  raise
actual_requests.heartbeat=actual_heartbeat
async def actual_preparation_boundary(stage,request_id,child_id):
 selected=root/'interrupt-preparation';marker=root/'actual-validation-preparation.json'
 if selected.exists() and selected.read_text()==stage and not marker.exists():
  pending=marker.with_suffix('.pending')
  with pending.open('x',encoding='utf8') as stream:
   json.dump({'stage':stage,'request_id':request_id,'child_id':child_id,'actual_stage_returned':True},stream);stream.flush();os.fsync(stream.fileno())
  os.replace(pending,marker)
  if (root/'disconnect-validation').exists() or (root/'cancel-original-job').exists():
   until=actual_time.monotonic()+30
   while not (root/'release-preparation').exists():
    if actual_time.monotonic()>=until:raise TimeoutError('Actual preparation suspension was not released')
    await asyncio.sleep(.02)
  else:await asyncio.Event().wait()
original_request_reserve=actual_requests.reserve
async def actual_request_reserve(owner,*args,**kwargs):
 value=await original_request_reserve(owner,*args,**kwargs)
 async with SessionLocal() as db:selected=await db.get(NativeRequest,owner.id)
 if selected.kind=='job.finalize':await actual_preparation_boundary('reserved',owner.id,args[1])
 return value
actual_requests.reserve=actual_request_reserve
original_cancel_create=actual_requests.create
async def actual_cancel_create(case_id,user_id,kind,settings,*,db=None):
 value=await original_cancel_create(case_id,user_id,kind,settings,db=db)
 if kind=='job.finalize' and db is not None:db.info['actual_finalizer_owner_committed']=value.id
 return value
actual_requests.create=actual_cancel_create
original_cancel_commit=AsyncSession.commit
async def actual_cancel_commit(db):
 value=await original_cancel_commit(db)
 request_id=db.info.pop('actual_finalizer_owner_committed',None)
 if request_id is not None:await actual_preparation_boundary('request',request_id,None)
 return value
AsyncSession.commit=actual_cancel_commit
original_scope_write=actual_scopes.write
async def actual_scope_write(owner,child_id,stage,*args,**kwargs):
 value=await original_scope_write(owner,child_id,stage,*args,**kwargs)
 await actual_preparation_boundary(stage,owner.id,child_id)
 return value
actual_scopes.write=actual_scope_write
original_root_release=job_execution.release_process
async def actual_root_release(owner,process):
 value=await original_root_release(owner,process)
 with (root/'actual-root-releases.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id})+'\n');stream.flush();os.fsync(stream.fileno())
 return value
job_execution.release_process=actual_root_release
from app.services import job_log_storage as actual_log_storage
def actual_io_boundary(stage):
 selected=root/'interrupt-finalizing-io';marker=root/'actual-finalizing-io.json'
 if selected.exists() and selected.read_text()==stage and not marker.exists():
  actual_mixed_marker(marker,{'stage':stage,'actual_io_returned':True,'owner':identity.observe(os.getpid())})
  until=actual_time.monotonic()+45
  while not (root/'release-preparation').exists():
   if actual_time.monotonic()>=until:raise TimeoutError('Actual finalization IO suspension was not released')
   actual_time.sleep(.02)
original_io_prepare=full_recovery.objects.prepare_runtime_cache
def actual_io_prepare(*args,**kwargs):
 value=original_io_prepare(*args,**kwargs);actual_io_boundary('cache_prepared');return value
full_recovery.objects.prepare_runtime_cache=actual_io_prepare
original_output_proof=full_recovery.outputs.proof
def actual_output_proof(*args,**kwargs):
 if (root/'legacy-output-protocol').exists():kwargs['schema']=full_recovery.outputs.LEGACY_SCHEMA
 return original_output_proof(*args,**kwargs)
full_recovery.outputs.proof=actual_output_proof
original_io_admit=actual_log_storage.admit
async def actual_io_admit(*args,**kwargs):
 value=await original_io_admit(*args,**kwargs)
 if (root/'interrupt-finalizing-io').exists() and (root/'interrupt-finalizing-io').read_text()=='log_admitted':
  await asyncio.to_thread(actual_io_boundary,'log_admitted')
 return value
actual_log_storage.admit=actual_io_admit
original_io_put=actual_log_storage.put_new
def actual_io_put(*args,**kwargs):
 value=original_io_put(*args,**kwargs);actual_io_boundary('log_put_returned');return value
actual_log_storage.put_new=actual_io_put
'''

PAUSE=r'''
import json,os,runpy,sys,time
from pathlib import Path
marker,probe=Path(sys.argv[1]),sys.argv[2]
assert sys.argv[3]=='--'
sys.argv=sys.argv[4:]
try:runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as error:
 if error.code not in (None,0):raise
# Forward the real compiler first, then hold this actual supervised process.
observe=runpy.run_path(probe)['observe']
pending=marker.with_suffix('.pending')
with pending.open('x',encoding='utf8') as stream:
 json.dump({'actual_helper_completed':True,'birth':observe(os.getpid())},stream);stream.flush();os.fsync(stream.fileno())
os.replace(pending,marker)
time.sleep(60)
'''


class CounterexampleComplete(Exception):pass


def verify(handler,boundary,*,database_url=None,interrupt_validation=False,before_fix=False,preparation_boundary=None,trace_transactions=False,disconnect_validation=False,repeat_disconnect=0,mixed_error=False,source_profile_id=None,cancel_original_job=False,two_apis=False,io_boundary=None,cancel_before_finalize=False,legacy_output_protocol=False,inspect_pending_archive=False,pending_archive_cancel=None,budget_recovery=None):
    from app.services import process_identity_native as identity
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed')
    api=IsolatedServer('professional-finalizing',app='output_fixture:app',database_url=database_url)
    interrupted=interrupt_validation or preparation_boundary is not None or mixed_error or io_boundary is not None
    report={'passed':False,'handler':handler,'terminal_boundary':boundary,'interrupt_validation':interrupt_validation,'preparation_boundary':preparation_boundary,'before_fix':before_fix,'database':'postgresql' if database_url else 'sqlite',
        'model_calls':0,'mock_llm_calls':0,'developer_database_modified':False,'API_births':[],'checks':[],
        'disconnect_validation':disconnect_validation,'repeat_disconnect':repeat_disconnect,'mixed_error':mixed_error,'cancel_original_job':cancel_original_job,'two_apis':two_apis,'io_boundary':io_boundary,'cancel_before_finalize':cancel_before_finalize,'legacy_output_protocol':legacy_output_protocol,'inspect_pending_archive':inspect_pending_archive,'pending_archive_cancel':pending_archive_cancel,'budget_recovery':budget_recovery}
    api_handles={};exit_witnesses={};peer_tokens={};model_configuration=None;model_nonce=None;live_fixture=''
    if source_profile_id is not None:
        from live_llm_acceptance import source,FIXTURE as actual_live_fixture
        model_configuration=asyncio.run(source(source_profile_id));model_nonce='RECOVERED_'+uuid.uuid4().hex;live_fixture=actual_live_fixture
    def save():
        trace=api.root/'upstream.jsonl'
        if model_configuration is not None and trace.exists():
            report['actual_model_upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['model_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in report['actual_model_upstream'])
        names=[*SOURCES,'attempt_professional_finalizing_acceptance.py']
        if source_profile_id is not None:names+=['live_llm_mixed_finalizer_acceptance.py','live_llm_acceptance.py','app/services/llm.py']
        report['source_hashes']={p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in names}
        raw=json.dumps(report,indent=2)+'\n'
        if model_configuration is not None:raw=raw.replace(model_configuration['key'],'[REDACTED]')
        (api.root/'professional-result.json').write_text(raw,encoding='utf8')
    def call(method,path,payload=None,expect=200,*,base=None,include_status=False):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+peer_tokens.get(base or api.base,api.token)},timeout=180,trust_env=False) as client:
            try:r=client.request(method,path,json=payload)
            except httpx.TransportError:
                if method=='POST' and path.endswith(':finalize-outputs'):
                    try:
                        state=client.get('/test/output-state/'+str(report['job_id']),timeout=10)
                        if state.status_code==200:report['actual_state_after_transport_error']=state.json();save()
                    except httpx.TransportError:pass
                raise
            try:value=r.json()
            except ValueError:value={'body':r.text[:500]}
            assert r.status_code in (expect if isinstance(expect,(list,tuple)) else (expect,)),(method,path,r.status_code,str(value)[:1000]);return (r.status_code,value) if include_status else value
    def exited(birth):
        key=json.dumps(birth,sort_keys=True)
        if key in exit_witnesses:return exit_witnesses[key]['observation']
        handle=api_handles.get(key)
        if handle is not None:
            handle.wait_exit(10)
            assert handle.identity(allow_exited=True)==birth
            value={'state':'exited','reason':'retained_creation_handle'}
            witness={'birth':birth,'observation':value,'observer':identity.observe(__import__('os').getpid()),'observed_at_unix':time.time()}
            exit_witnesses[key]=witness;report['API_exit_witnesses']=list(exit_witnesses.values());save();return value
        until=time.monotonic()+10
        while True:
            value=identity.probe(birth)
            if value=={'state':'exited','reason':'process_exited'}:
                witness={'birth':birth,'observation':value,'observer':identity.observe(__import__('os').getpid()),'observed_at_unix':time.time()}
                exit_witnesses[key]=witness;report['API_exit_witnesses']=list(exit_witnesses.values());save();return value
            assert time.monotonic()<until,value;time.sleep(.03)
    def remember_api(*,base=None):
        from app.services.windows_process_scope import ProcessHandle
        birth=call('GET','/test/output-process',base=base);handle=ProcessHandle.query(birth)
        assert handle.identity()==birth
        api_handles[json.dumps(birth,sort_keys=True)]=handle
        report['API_births'].append(birth);save();return birth
    @contextlib.contextmanager
    def peer():
        if not two_apis:yield api.base;return
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port)
        report.setdefault('peer_apis',[]).append(base);save()
        with (api.root/('peer-'+str(port)+'.log')).open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','output_fixture:app','--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            birth=None
            try:
                until=time.monotonic()+30
                with httpx.Client(trust_env=False,timeout=1) as client:
                    while True:
                        assert proc.poll() is None,'Actual peer API launcher exited during startup'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        assert time.monotonic()<until,'Actual peer API did not become ready';time.sleep(.1)
                peer_tokens[base]=api.token
                birth=remember_api(base=base);yield base
            finally:
                try:
                    if birth is not None:
                        try:call('POST','/test/output-exit',base=base)
                        except httpx.TransportError:pass
                        else:raise AssertionError('Actual peer API exit returned HTTP')
                        exited(birth)
                finally:
                    peer_tokens.pop(base,None)
                    if proc.poll() is None:proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    def compete(path,payload,base):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(call,'POST',path,payload,(200,409),base=at,include_status=True) for at in (api.base,base)]
            outcomes=[future.result() for future in futures]
        report.setdefault('actual_competing_finalizations',[]).append(outcomes);save()
        winners=[value for status,value in outcomes if status==200]
        assert winners,outcomes
        for status,value in outcomes:
            if status==409:assert value['detail']['code'] in {'recovery_required','state_conflict','output_archive_busy','storage_quota_busy','output_recovery_busy'},outcomes
            else:assert value==winners[0],outcomes
        assert call('POST',path,payload)==call('POST',path,payload,base=base)==winners[0]
        return winners[0]
    def open_request(path,payload):
        from urllib.parse import urlsplit
        target=urlsplit(api.base);body=json.dumps(payload).encode()
        selected=socket.create_connection((target.hostname,target.port),timeout=10)
        headers=('POST /api/v1'+path+' HTTP/1.1\r\nHost: '+target.netloc+'\r\nAuthorization: Bearer '+api.token+'\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()
        selected.sendall(headers+body);return selected
    def wait_cancelled(job_id):
        until=time.monotonic()+10
        while not call('GET','/test/output-cancellation-observed/'+str(job_id))['actual_operation_cancelled']:
            assert time.monotonic()<until,'ASGI did not cancel its actual disconnected operation';time.sleep(.02)
    def restart(label):
        birth=call('GET','/test/output-process')
        try:call('POST','/test/output-exit')
        except httpx.TransportError:pass
        else:raise AssertionError('Actual API exit returned HTTP')
        exited(birth);api.__exit__(RuntimeError,None,None)
        shutil.copyfile(api.root/'api.log',api.root/(label+'.log'))
        api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
        remember_api()
    print('PROFESSIONAL_FINALIZING '+str(api.root)+' STORE '+str(store.root),flush=True)
    disconnected_socket=None
    try:
        with store:
            if budget_recovery:(api.root/'budget-recovery-mode').write_text(budget_recovery,encoding='utf8')
            if pending_archive_cancel=='unadmitted':(api.root/'pause-original-archive-admission').write_text('Pause actual admission before any SDK write or reservation\n',encoding='utf8')
            if legacy_output_protocol:(api.root/'legacy-output-protocol').write_text('Actual original native/S3 run using legacy proof format; do not upgrade it\n',encoding='utf8')
            if cancel_original_job:(api.root/'cancel-original-job').write_text('Actual original Job cancellation during real validation; keep HTTP request connected\n',encoding='utf8')
            if mixed_error:(api.root/'mixed-error-after-finalizer').write_text('Forward actual compiler, then actual missing-file OS error; cancel during real cleanup\n',encoding='utf8')
            if disconnect_validation:(api.root/'disconnect-validation').write_text('Real client TCP shutdown; API remains alive\n',encoding='utf8')
            if trace_transactions:(api.root/'trace-native-transactions').write_text('Actual transaction boundaries and statement classes only; no SQL parameters\n',encoding='utf8')
            (api.root/'native-handler').write_text(handler,encoding='utf8')
            if preparation_boundary:(api.root/'interrupt-preparation').write_text(preparation_boundary,encoding='utf8')
            if interrupt_validation:
                (api.root/'interrupt-validation').write_text('Forward real compiler, then pause actual owned helper\n',encoding='utf8')
                (api.root/'actual-validation-pause.py').write_text(PAUSE,encoding='utf8')
            (api.root/'output_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+'\n'+EXTRA+'\n'+live_fixture,encoding='utf8')
            with api:
                api.create_case('Actual '+handler+' interrupted finalization');user=api.call('GET','/auth/me')
                remember_api()
                if handler=='report':
                    api.put('E-report/report.md','# Original recovery document\n\nActual Pandoc output.\n\n$x^2+1$\n'+('\n'+model_nonce+'\n' if model_nonce else ''))
                    submitted=call('POST',api.prefix+'/report:build',{})
                else:
                    api.put('E-report/latex/main.tex',r'\documentclass{article}'+'\n'+r'\begin{document}Original recovery document: $x^2+1$.\end{document}'+'\n')
                    submitted=call('POST',api.prefix+'/latex:build',{'recipe':'pdflatex'})
                job_id=submitted['job_id'];report['job_id']=job_id;report['original_submission']=submitted;save()
                marker=api.root/('actual-archive-admission-boundary.json' if pending_archive_cancel=='unadmitted' else 'actual-output-upload-boundary.json');until=time.monotonic()+150
                while not marker.exists():
                    job=call('GET','/jobs/'+str(job_id))
                    if job['status'] in {'failed','cancelled','interrupted','succeeded'}:
                        report['unexpected_original_job']=job;report['unexpected_original_state']=call('GET','/test/output-state/'+str(job_id));save()
                    assert job['status'] not in {'failed','cancelled','interrupted','succeeded'},job['status']
                    assert time.monotonic()<until,'Original real native document did not reach output upload';time.sleep(.1)
                captured=call('GET','/test/output-state/'+str(job_id));report['before_exit']=captured;save()
                assert captured['phase']=='finalizing' and captured['output']['object_store']['state']=='pending'
                assert captured['output']['output_preparation']['stop']['verified'] and captured['output']['finalizing_source']['pre_capture_stages_returned']
                releases=[json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]
                assert releases==[{'job_id':job_id,'attempt_id':captured['attempt_id']}]
                restart('original-owner')
                options=call('GET','/jobs/'+str(job_id)+'/output-recovery')
                payload={'attempt_id':captured['attempt_id'],'inventory_sha256':options['inventory_sha256']}
                if pending_archive_cancel:
                    full_path='/jobs/'+str(job_id)+':finalize-outputs';copy_socket=None
                    with peer() as peer_base:
                        if pending_archive_cancel in {'drain','exit'}:
                            call('POST','/test/output-arm-cancel')
                            copy_socket=open_request('/jobs/'+str(job_id)+':recover-outputs',payload)
                            until=time.monotonic()+60
                            while not call('GET','/test/output-cancel-status')['entered']:
                                assert time.monotonic()<until,'Actual copy did not reach its protected S3 boundary';time.sleep(.05)
                            report['actual_copy_paused']=call('GET','/test/output-state/'+str(job_id));save()
                        if budget_recovery=='expired':
                            while time.time()<=captured['original_deadline_unix']+.1:time.sleep(.1)
                            report['actual_original_deadline_expired_at_request']=time.time();save()
                        cancelled=call('POST','/jobs/'+str(job_id)+':cancel',base=peer_base);assert cancelled['status']=='cancelled'
                        before_cancel=call('GET','/test/output-state/'+str(job_id));report['pending_archive_before_cancel_finish']=before_cancel;save()
                        if copy_socket is not None:
                            refused=call('POST',full_path,payload,expect=409,base=peer_base)
                            assert refused['detail']['code']=='output_archive_busy',refused
                            waiting=call('GET','/test/output-state/'+str(job_id))
                            assert waiting['phase']=='cancelling' and waiting['write_checkpoint']=={} and not waiting['finalization_requests']
                            report['live_copy_refusal']=refused;save()
                            if pending_archive_cancel=='drain':
                                report['actual_copy_drain']=call('POST','/test/output-cancel-now')
                                assert report['actual_copy_drain']['actual_thread_drained']
                                report['actual_copy_HTTP_status']=copy_socket.recv(4096).split(b'\r\n',1)[0].decode('ascii')
                            else:restart('pending-copy-owner')
                            copy_socket.close();copy_socket=None
                        result=compete(full_path,payload,peer_base) if two_apis else call('POST',full_path,payload)
                        done=call('GET','/test/output-state/'+str(job_id));report['pending_archive_final']=done;report['pending_archive_response']=result;save()
                        assert result['state']==done['phase']==done['job_status']=='cancelled' and not result['finalization_pending']
                        assert not result['native_execution_started'] and not result['native_validation_started'] and not result['solver_execution_started']
                        assert done['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v6' and done['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                        assert done['input']==captured['input'] and done['attempt_id']==captured['attempt_id'] and done['original_deadline_unix']==captured['original_deadline_unix']
                        assert done['output']['output_preparation']==captured['output']['output_preparation'] and done['output']['object_store']==captured['output']['object_store']
                        assert done['log_pointer']==captured['log_pointer'] and done['remote_log']==captured['remote_log']
                        assert done['reservation']==captured['reservation']
                        if pending_archive_cancel=='unadmitted':assert done['reservation'] is None
                        else:assert done['reservation']['state']=='active' and done['reservation']['remaining_bytes']==done['reservation']['size']
                        assert not done['finalization_requests'] and not done['finalizer_evidence']
                        assert done['publication_state']['status']=='failed' and done['publication_state']['current_id']==captured['publication_state']['current_id']
                        copy_events=[e for e in done['copy_events'] if e['type']=='attempt.cancelled_archive_copy_closed'];assert len(copy_events)==1
                        assert copy_events[0]['payload']['method']==('request_drained' if pending_archive_cancel=='drain' else 'owner_exited' if pending_archive_cancel=='exit' else 'not_admitted' if pending_archive_cancel=='unadmitted' else 'original_owner_exited')
                        assert not [e for e in done['copy_events'] if e['type']=='attempt.outputs_recovered']
                        options=call('GET','/jobs/'+str(job_id)+'/output-recovery');assert options['state']=='cancelled' and options['archive_pending'] and not options['finalization_pending']
                        late=call('POST','/jobs/'+str(job_id)+':recover-outputs',payload,expect=409)
                        assert late['detail']['code']=='cancelled_archive_pending'
                        assert call('POST',full_path,payload)==result
                        unchanged=call('GET','/test/output-state/'+str(job_id))
                        assert unchanged['copy_events']==done['copy_events'] and unchanged['terminal_events']==done['terminal_events']
                        report['pending_archive_options']=options;report['late_upload_refusal']=late
                    assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                    report['checks'].append({'check':'actual_pending_archive_cancel_preserves_unknown_bytes_and_original_identity_without_native_or_publication','mode':pending_archive_cancel})
                    report['functional_passed']=True;save();raise CounterexampleComplete()
                if inspect_pending_archive:
                    cancelled=call('POST','/jobs/'+str(job_id)+':cancel');assert cancelled['status']=='cancelled'
                    diagnosis=call('GET','/jobs/'+str(job_id)+'/output-recovery')
                    assert diagnosis['state']=='requires_attention' and diagnosis['reason']=='cancelled_output_archive_pending' and diagnosis['archive_pending'] and not diagnosis['automatic_replay']
                    refused=call('POST','/jobs/'+str(job_id)+':finalize-outputs',payload,expect=409)
                    assert refused['detail']['code']=='cancelled_archive_pending'
                    state=call('GET','/test/output-state/'+str(job_id));assert state['job_status']=='cancelled' and state['phase']=='cancelling' and state['write_checkpoint']=={}
                    assert not state['finalization_requests'] and not state['finalizing_io_operations'] and state['output']['object_store']['state']=='pending'
                    assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                    report.update(pending_archive_diagnosis=diagnosis,pending_archive_refusal=refused,pending_archive_state=state,functional_passed=True)
                    report['checks'].append({'check':'actual_pending_archive_cancellation_diagnosed_without_claiming_completion_or_replay'});save();raise CounterexampleComplete()
                archive=call('POST','/jobs/'+str(job_id)+':recover-outputs',payload);assert archive['state']=='archived'
                report['actual_cache_loss']=call('POST','/test/output-drop-run',{'job_id':job_id,'whole_attempt':True});save()
                if cancel_before_finalize:
                    full_path='/jobs/'+str(job_id)+':finalize-outputs'
                    before_cancel=call('GET','/test/output-state/'+str(job_id));assert not before_cancel['finalizing_io_operations'] and not before_cancel['finalization_requests']
                    cancelled=call('POST','/jobs/'+str(job_id)+':cancel');assert cancelled['status']=='cancelled'
                    with peer() as peer_base:
                        response=call('POST',full_path,payload,expect=(200,409),base=peer_base)
                        state=call('GET','/test/output-state/'+str(job_id))
                        report.update(before_first_finalize=before_cancel,actual_first_finalize_cancellation=cancelled,first_finalize_response=response,first_finalize_state=state);save()
                        if before_fix or legacy_output_protocol:
                            assert response['detail']['code']==('legacy_finalizing_io_unverified' if legacy_output_protocol else 'recovery_required') and state['phase']=='cancelling' and state['write_checkpoint']=={}
                            assert not state['finalization_requests']
                            if legacy_output_protocol:
                                assert state['output']['output_preparation']['schema']=='vibeflow.attempt-output-preparation.v1'
                                original_receipt=next(e for e in state['copy_events'] if e['type']=='attempt.outputs_recovered')
                                assert original_receipt['payload']['schema']=='vibeflow.attempt-output-recovery.v2'
                                options=call('GET','/jobs/'+str(job_id)+'/output-recovery');notice=options['cancellation_recovery']['notice']
                                assert notice['state']=='requires_attention' and notice['reason']=='finalizing_io_unverified' and notice['next_step']=='manual_review' and not notice['automatic_replay']
                                assert call('POST',full_path,payload,expect=409)['detail']['code']=='legacy_finalizing_io_unverified'
                                assert call('GET','/jobs/'+str(job_id)+'/output-recovery')['cancellation_recovery']['notice']['event_id']==notice['event_id']
                                report['legacy_cancellation_diagnosis']=options
                        else:
                            assert response['state']==state['phase']==state['job_status']=='cancelled' and not response['native_validation_started'] and not response['finalization_pending']
                            assert state['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5' and state['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                            assert state['input']==captured['input'] and state['original_deadline_unix']==captured['original_deadline_unix']
                            assert state['publication_state']['status']=='failed' and state['publication_state']['current_id']==captured['publication_state']['current_id']
                            assert len(state['finalization_requests'])==1
                            assert state['output']['output_preparation']['schema']=='vibeflow.attempt-output-preparation.v2'
                            modern_receipt=next(e for e in state['copy_events'] if e['type']=='attempt.outputs_recovered')
                            assert modern_receipt['payload']['schema']=='vibeflow.attempt-output-recovery.v3' and modern_receipt['payload']['recovery_protocol']=='vibeflow.finalizing-io.v1'
                            request=state['finalization_requests'][0];assert request['phase']=='failed' and request['error']=='OriginalJobCancelled' and not request['children'].get('children') and request['result']['prepared'] is None
                            assert call('POST',full_path,payload)==response
                            if two_apis:assert compete(full_path,payload,peer_base)==response
                            cache_state=call('GET','/test/output-cache-state/'+str(job_id));assert not cache_state['run_exists'] and not cache_state['attempt_exists']
                            report['first_finalize_cache_still_missing']=cache_state
                    assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                    report['checks'].append({'check':'actual_original_Job_cancel_before_any_finalizing_IO_no_cache_or_compiler','counterexample':before_fix})
                    report['functional_passed']=True;save();raise CounterexampleComplete()
                if io_boundary:(api.root/'interrupt-finalizing-io').write_text(io_boundary,encoding='utf8')
                if not interrupted:call('POST','/test/output-arm-terminal',{'stage':boundary})
                full_path='/jobs/'+str(job_id)+':finalize-outputs'
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    if disconnect_validation:
                        disconnected_socket=open_request(full_path,payload);pending=None
                    else:pending=pool.submit(call,'POST',full_path,payload,(200,409) if cancel_original_job else 200)
                    marker=api.root/('actual-budget-diagnostic-boundary.json' if budget_recovery else 'actual-finalizing-io.json' if io_boundary else 'actual-mixed-close.json' if mixed_error else 'actual-validation-preparation.json' if preparation_boundary else 'actual-validation-running.json' if interrupt_validation else 'actual-full-finalization-boundary.json');until=time.monotonic()+180
                    while not marker.exists():
                        if pending is not None:assert not pending.done(),pending.result()
                        assert time.monotonic()<until,'Independent native validation did not reach terminal commit';time.sleep(.1)
                    reached=json.loads(marker.read_text())
                    if interrupted:
                        report['validation_inflight']=call('GET','/test/output-state/'+str(job_id))
                        inflight=report['validation_inflight']['finalization_requests']
                        if io_boundary:
                            assert reached['actual_io_returned'] and reached['stage']==io_boundary and not inflight
                            report['actual_io_interruption']=reached
                            if not before_fix:
                                active_io=report['validation_inflight']['finalizing_io_operations']
                                assert len(active_io)==1 and active_io[0]['state']=='active' and active_io[0]['owner']==reached['owner']
                        elif mixed_error:
                            assert reached['actual_native_finalizer_returned'] and reached['actual_close_entered'] and reached['error']=='FileNotFoundError'
                            assert inflight[0]['id']==reached['request_id']
                            assert inflight[0]['children']['children'] and all(c['stop']['verified'] for c in inflight[0]['children']['children'].values())
                            report['actual_mixed_error']=reached
                        elif preparation_boundary:
                            assert reached['actual_stage_returned'] and reached['stage']==preparation_boundary
                            if preparation_boundary=='request':assert not inflight[0]['children'].get('children')
                            else:
                                child=inflight[0]['children']['children'][reached['child_id']]
                                assert child['phase']=='preparing' and child['identity'] is None
                            report['actual_preparation_crash']=reached
                        else:
                            assert reached['actual_helper_completed']
                            assert any(c['phase']=='running' and c['identity'] for c in inflight[0]['children']['children'].values())
                            report['actual_validator_crash']=reached
                        if not io_boundary:assert len(inflight)==1 and inflight[0]['phase']=='active'
                    else:
                        assert reached['stage']==boundary and reached['status']=='succeeded'
                        report['actual_terminal_crash']=reached
                    save()
                    if cancel_original_job:
                        owner_birth=report['API_births'][-1]
                        with peer() as peer_base:
                            if two_apis:
                                refused=call('POST',full_path,payload,expect=409,base=peer_base)
                                expected_busy='output_recovery_busy' if any(r['state']=='active' for r in report['validation_inflight'].get('finalizing_io_operations',[])) else 'recovery_required'
                                assert refused['detail']['code']==expected_busy
                                report['live_owner_peer_refusal']=refused;save()
                            cancelled=call('POST','/jobs/'+str(job_id)+':cancel',base=peer_base);assert cancelled['ok'] and cancelled['status']=='cancelled'
                            if io_boundary and not before_fix:
                                io_cancelling=call('GET','/test/output-state/'+str(job_id))
                                assert io_cancelling['phase']=='cancelling' and not pending.done() and not io_cancelling['finalization_requests']
                                assert io_cancelling['finalizing_io_operations']==report['validation_inflight']['finalizing_io_operations']
                                report['actual_IO_still_owned_after_Job_cancel']=io_cancelling;save()
                            (api.root/'release-preparation').write_text('Actual Job cancel committed; let protected preparation drain\n',encoding='utf8')
                            outcome=pending.result(timeout=120);drained=call('GET','/test/output-state/'+str(job_id))
                            assert identity.probe(owner_birth)['state']=='match'
                            if two_apis and not before_fix:assert compete(full_path,payload,peer_base)==outcome
                        report.update(original_job_cancel=cancelled,original_job_cancel_response=outcome,original_job_cancel_state=drained);save()
                        assert drained['job_status']=='cancelled' and drained['attempt_id']==captured['attempt_id']
                        assert all(r['phase']!='active' for r in drained['finalization_requests'])
                        if before_fix:
                            assert drained['phase']=='cancelling' and drained['write_checkpoint']=={}
                            refused=call('POST',full_path,payload,expect=409)
                            assert refused['detail']['code'] in {'state_conflict','output_recovery_unavailable'}
                            assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                            report.update(before_fix_refusal=refused,before_fix_state=drained,functional_passed=True);save();raise CounterexampleComplete()
                        assert outcome['state']=='cancelled' and drained['phase']=='cancelled'
                        assert call('POST',full_path,payload)==outcome
                        assert drained['input']==captured['input'] and drained['original_deadline_unix']==captured['original_deadline_unix']
                        assert len(drained['finalization_requests'])==1
                        terminal_request=drained['finalization_requests'][0]
                        if not io_boundary:
                            original_request=inflight[0]
                            assert terminal_request['id']==original_request['id'] and terminal_request['profile']==original_request['profile'] and terminal_request['owner_identity']==original_request['owner_identity']
                            assert terminal_request['result']['usage']['observed_output_bytes']>=original_request['result']['usage']['observed_output_bytes']
                            assert terminal_request['result']['usage'].get('unsettled_output_bytes',0)>=original_request['result']['usage'].get('unsettled_output_bytes',0)
                        else:assert not terminal_request['children'].get('children') and not outcome['native_validation_started']
                        if io_boundary:
                            assert terminal_request['phase']=='failed' and terminal_request['error']=='OriginalJobCancelled' and terminal_request['result']['prepared'] is None
                            assert len(drained['finalizing_io_operations'])==1 and drained['finalizing_io_operations'][0]['state']=='settled'
                            proofs=[e['payload'] for e in drained['finalizing_io_evidence'] if e['type']=='job.finalizing_io_drained']
                            assert len(proofs)==1 and proofs[0]['io_drained'] is True
                            assert not [e for e in drained['finalizing_io_evidence'] if e['type']=='job.finalizing_io_owner_exited']
                        preparation_closures=[e['payload'] for e in drained['finalizer_evidence'] if e['type']=='job.finalizer_preparation_closed']
                        children=terminal_request['children'].get('children',{})
                        assert {p['binding']['child_id'] for p in preparation_closures}=={key for key,child in children.items() if child['identity'] is None}
                        for proof in preparation_closures:
                            assert proof['schema']=='vibeflow.finalizer-preparation-closure.v3' and proof['business_not_released'] is True and proof['unknown_process_exit_inferred'] is False
                            assert 'owner_exit' not in proof and children[proof['binding']['child_id']]['stop'] is None
                            if proof['native'] is not None:assert proof['native']['result']['state']=='absent'
                            if proof['guardian'] is not None:
                                receipt=proof['guardian']['receipt'];assert receipt['verified'] and receipt['sealed'] and receipt['active_processes']==0
                        for child in children.values():
                            if child['identity'] is not None:
                                stop=child['stop'];assert stop['verified'] and stop['scope_empty'] and stop['root_exit_code'] is not None
                                if child['identity']['native'] is not None:assert stop['native_state']=='exited'
                                if stop['root_exit_code']==0:assert stop['control_valid']
                        assert drained['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5' and drained['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                        assert drained['publication_state']['status']=='failed' and drained['publication_state']['id']==submitted['publication_id']
                        assert drained['publication_state']['current_id']==captured['publication_state']['current_id']
                        assert not outcome['solver_execution_started'] and not outcome['finalization_pending']
                        assert sum(e['payload'].get('status')=='cancelled' for e in drained['job_status_events'])==1
                        assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                        report['checks'].append({'check':'actual_original_Job_cancel_drains_validator_and_settles_original_attempt_without_replay'})
                        report['functional_passed']=True;save();raise CounterexampleComplete()
                    elif disconnect_validation:
                        report['API_alive_before_disconnect']=identity.probe(report['API_births'][-1]);assert report['API_alive_before_disconnect']['state']=='match'
                        disconnected_socket.shutdown(socket.SHUT_RDWR);disconnected_socket.close();disconnected_socket=None
                        wait_cancelled(job_id)
                        (api.root/'release-preparation').write_text('Actual client disconnected; allow protected preparation to drain\n',encoding='utf8')
                        if mixed_error and before_fix:
                            rejected=api.root/'actual-mixed-completion-rejection.json';until=time.monotonic()+90
                            while not rejected.exists():
                                assert time.monotonic()<until,'Mixed terminal rejection was not observed';time.sleep(.1)
                            failure=json.loads(rejected.read_text());assert failure['error']=='ValueError'
                            unchanged=call('GET','/test/output-state/'+str(job_id));row=unchanged['finalization_requests'][0]
                            assert row['phase']=='active' and len(unchanged['finalization_requests'])==1
                            refused=call('POST',full_path,payload,expect=409);assert refused['detail']['code']=='recovery_required'
                            assert identity.probe(report['API_births'][-1])['state']=='match'
                            report.update(before_fix_refusal=refused,before_fix_state=unchanged,actual_mixed_terminal_rejection=failure,functional_passed=True)
                            save();raise CounterexampleComplete()
                        until=time.monotonic()+90
                        while True:
                            drained=call('GET','/test/output-state/'+str(job_id));old=drained['finalization_requests'][0]
                            if old['phase']!='active':break
                            assert time.monotonic()<until,'Disconnected validator did not finish cancelling';time.sleep(.1)
                        assert old['phase']=='cancelled' and old['error']==('FileNotFoundError' if mixed_error else 'CancelledError')
                        assert drained['phase']=='finalizing' and drained['job_status']=='running'
                        report['cancelled_state']=drained;report['API_alive_after_disconnect']=identity.probe(report['API_births'][-1])
                        assert report['API_alive_after_disconnect']['state']=='match';save()
                    else:
                        restart('validation-owner' if interrupted else 'terminal-owner')
                        try:pending.result()
                        except httpx.TransportError:pass
                        else:raise AssertionError('Killed terminal response unexpectedly returned')
                if disconnect_validation and not before_fix:
                    report['cancelled_states']=[report['cancelled_state']]
                    for iteration in range(repeat_disconnect):
                        marker.rename(marker.with_name(marker.stem+'-prior-'+str(iteration)+marker.suffix))
                        release=api.root/'release-preparation'
                        if release.exists():release.unlink()
                        disconnected_socket=open_request(full_path,payload);until=time.monotonic()+180
                        while not marker.exists():
                            assert time.monotonic()<until,'Repeated actual validation did not reach its interruption boundary';time.sleep(.1)
                        inflight=call('GET','/test/output-state/'+str(job_id));active=[r for r in inflight['finalization_requests'] if r['phase']=='active']
                        assert len(active)==1
                        for previous in report['cancelled_states'][-1]['finalization_requests']:
                            assert next(r for r in inflight['finalization_requests'] if r['id']==previous['id'])==previous
                        disconnected_socket.shutdown(socket.SHUT_RDWR);disconnected_socket.close();disconnected_socket=None
                        wait_cancelled(job_id)
                        release.write_text('Repeated real client disconnect; drain actual protected preparation\n',encoding='utf8')
                        until=time.monotonic()+90
                        while True:
                            drained=call('GET','/test/output-state/'+str(job_id));old=next(r for r in drained['finalization_requests'] if r['id']==active[0]['id'])
                            if old['phase']!='active':break
                            assert time.monotonic()<until,'Repeated disconnect did not finish cancelling';time.sleep(.1)
                        assert old['phase']=='cancelled' and old['error']=='CancelledError' and drained['phase']=='finalizing'
                        assert identity.probe(report['API_births'][-1])['state']=='match'
                        report['cancelled_states'].append(drained);save()
                if before_fix:
                    if disconnect_validation:
                        refused=call('POST',full_path,payload)
                        assert refused['state']=='failed' and not refused['solver_execution_started']
                        unchanged=call('GET','/test/output-state/'+str(job_id))
                        assert unchanged['attempt_id']==captured['attempt_id'] and unchanged['reason_code']=='publication_failed'
                        assert len(unchanged['finalization_requests'])==1
                        report.update(before_fix_refusal=refused,before_fix_state=unchanged,functional_passed=True)
                        save();raise CounterexampleComplete()
                    refused=call('POST',full_path,payload,expect=409)
                    assert refused['detail']['code']==('finalization_recovery_unavailable' if preparation_boundary else 'recovery_required'),refused
                    unchanged=call('GET','/test/output-state/'+str(job_id))
                    assert unchanged['attempt_id']==captured['attempt_id'] and unchanged['phase']=='finalizing'
                    assert len(unchanged['finalization_requests'])==1 and unchanged['finalization_requests'][0]['phase']=='active'
                    if not preparation_boundary:assert unchanged['finalization_requests'][0]['result']=={}
                    assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                    report.update(before_fix_refusal=refused,before_fix_state=unchanged,functional_passed=True)
                    save();raise CounterexampleComplete()
                if budget_recovery=='expired':
                    while time.time()<=captured['original_deadline_unix']+.1:time.sleep(.1)
                    report['actual_original_deadline_expired_at_request']=time.time();save()
                with peer() as peer_base:
                    done=compete(full_path,payload,peer_base) if two_apis else call('POST',full_path,payload)
                if budget_recovery in {'expired','log_limit'}:
                    final=call('GET','/test/output-state/'+str(job_id));report.update(full_finalization=done,full_final_state=final);save()
                    assert done['state']==final['job_status']==final['phase']=='failed' and not done['finalization_pending'] and not done['native_execution_started']
                    assert final['reason_code']=='resource_limit' and final['input']==captured['input'] and final['attempt_id']==captured['attempt_id'] and final['original_deadline_unix']==captured['original_deadline_unix']
                    assert final['publication_state']['status']=='failed' and final['publication_state']['current_id']==captured['publication_state']['current_id']
                    assert final['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5' and final['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                    assert len(final['finalization_requests'])==2
                    request=next(r for r in final['finalization_requests'] if r['id']==final['write_checkpoint']['recovery']['request_id'])
                    previous=next(r for r in final['finalization_requests'] if r['id']!=request['id'])
                    # This metadata operation returned normally after observing
                    # the exhausted budget. Its original Job must be failed;
                    # no native validation or prepared publication succeeded.
                    assert request['phase']=='succeeded' and request['error'] is None and not request['children'].get('children') and request['result']['prepared'] is None
                    assert previous['phase']=='failed' and previous['error']=='OwnerExited'
                    assert request['result']['usage']['violation']==('timeout' if budget_recovery=='expired' else 'log_limit')
                    assert request['result']['usage'].get('unsettled_output_bytes')==previous['result']['usage'].get('unsettled_output_bytes')==4096
                    certificate=next(e['payload'] for e in final['finalizer_evidence'] if e['type']=='job.finalizer_owner_recovered')
                    observations=certificate['output_settlement']['diagnostic_observations']
                    assert sum(v['newly_observed_bytes'] for v in observations.values())==report['actual_validator_crash']['actual_diagnostic_growth']
                    assert request['result']['usage']['observed_diagnostic_bytes']>=previous['result']['usage']['observed_diagnostic_bytes']>=report['actual_validator_crash']['actual_diagnostic_growth']
                    assert call('POST',full_path,payload)==done and [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                    report['checks'].append({'check':'actual_diagnostic_growth_and_unknown_window_no_fresh_credit_after_limit_or_deadline','mode':budget_recovery,'unknown_output_bytes':4096,'diagnostic_growth':report['actual_validator_crash']['actual_diagnostic_growth']})
                    report['functional_passed']=True;save();raise CounterexampleComplete()
                assert done['state']=='succeeded' and done['native_validation_started'] and not done['solver_execution_started'] and not done['finalization_pending']
                assert call('POST',full_path,payload)==done
                final=call('GET','/test/output-state/'+str(job_id));report.update(full_finalization=done,full_final_state=final);save()
                if io_boundary:
                    assert all(r['state']=='settled' for r in final['finalizing_io_operations']) and len(final['finalizing_io_operations'])==2
                    exits=[e['payload'] for e in final['finalizing_io_evidence'] if e['type']=='job.finalizing_io_owner_exited']
                    assert len(exits)==1 and exits[0]['owner_exit']=={'state':'exited','reason':'process_exited'}
                assert final['input']==captured['input'] and final['attempt_id']==captured['attempt_id'] and final['job_status']=='succeeded'
                assert final['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5' and final['write_checkpoint']['stop']==captured['actual_in_memory_stop']
                assert len(final['finalization_requests'])==(2+repeat_disconnect if interrupted and not io_boundary else 1)
                request=next(r for r in final['finalization_requests'] if r['id']==final['write_checkpoint']['recovery']['request_id'])
                children=request['children']['children']
                if interrupted and not io_boundary:
                    previous=next(r for r in final['finalization_requests'] if r['id']!=request['id'])
                    original_request=report['validation_inflight']['finalization_requests'][0]
                    assert previous['id']==original_request['id'] and previous['phase']==('cancelled' if disconnect_validation else 'failed') and previous['error']==('FileNotFoundError' if mixed_error else 'CancelledError' if disconnect_validation else 'OwnerExited')
                    for key,child in original_request['children'].get('children',{}).items():
                        recovered=previous['children']['children'][key]
                        if disconnect_validation:
                            assert recovered==report['cancelled_state']['finalization_requests'][0]['children']['children'][key]
                        else:assert recovered['identity']==child['identity'] and recovered['stop']==child['stop']
                    usage=previous['result']['usage'];prior=original_request['result']['usage']
                    assert usage['observed_output_bytes']>=prior['observed_output_bytes']
                    expected_uncertain=0 if preparation_boundary or disconnect_validation else 4096
                    assert usage.get('unsettled_output_bytes',0)==expected_uncertain
                    assert request['result']['usage'].get('unsettled_output_bytes',0)==expected_uncertain
                    assert request['result']['usage']['observed_output_bytes']>=usage['observed_output_bytes']
                    assert final['original_deadline_unix']==captured['original_deadline_unix']
                    certificates=[e['payload'] for e in final['finalizer_evidence'] if e['type']==('job.finalizer_cancelled_drained' if disconnect_validation else 'job.finalizer_owner_recovered')]
                    assert len(certificates)==1+repeat_disconnect
                    certificate=next(c for c in certificates if c['request_id']==previous['id'])
                    if disconnect_validation:
                        assert all('owner_exit' not in c for c in certificates)
                        assert request['result']['usage']['observed_output_bytes']>=certificate['usage']['observed_output_bytes']
                        for old in report['cancelled_states'][-1]['finalization_requests']:
                            assert next(r for r in final['finalization_requests'] if r['id']==old['id'])==old
                    else:
                        assert certificate['owner_exit']=={'state':'exited','reason':'process_exited'}
                        assert certificate['output_settlement']['newly_retained_unknown_output_bytes']==expected_uncertain
                        if budget_recovery:
                            growth=sum(v['newly_observed_bytes'] for v in certificate['output_settlement']['diagnostic_observations'].values())
                            assert growth==report['actual_validator_crash']['actual_diagnostic_growth'] and usage['observed_diagnostic_bytes']>=growth
                            report['checks'].append({'check':'actual_unsampled_diagnostic_growth_charged_with_unknown_pipe_window','actual_diagnostic_growth':growth,'retained_unknown_output_bytes':expected_uncertain})
                    report['checks'].append({'check':'actual_HTTP_disconnect_same_attempt_retry_preserved_history_and_credit' if disconnect_validation else 'actual_validator_owner_exit_same_attempt_retry_preserved_missing_stop_and_retained_unknown_credit',
                        'previous_request_id':previous['id'],'request_id':request['id'],'unsettled_output_bytes':expected_uncertain})
                closures=[e['payload'] for e in final['finalizer_evidence'] if e['type']=='job.finalizer_scope_closed']
                all_children={key:child for r in final['finalization_requests'] for key,child in r['children'].get('children',{}).items()}
                preparation_closures=[e['payload'] for e in final['finalizer_evidence'] if e['type']=='job.finalizer_preparation_closed']
                assert {e['binding']['child_id'] for e in closures}|{e['binding']['child_id'] for e in preparation_closures}==set(all_children)
                for closed in preparation_closures:
                    child=all_children[closed['binding']['child_id']]
                    assert child['identity'] is None and child['stop'] is None and closed['business_not_released'] is True
                for closure in closures:
                    proof=closure['value'];receipt=proof['guardian']['receipt']
                    assert receipt['verified'] and receipt['sealed'] and receipt['active_processes']==0 and receipt['membership']['complete']
                    if handler=='latex':
                        assert proof['native']['result']['state']=='absent'
                        identity_value=all_children[closure['binding']['child_id']]['identity']
                        if identity_value is None:
                            assert closure['binding']['child_id'] in {c['binding']['child_id'] for c in preparation_closures}
                        else:assert identity_value['native']['process']['uid']==65534
                assert request['phase']=='succeeded' and children and all(c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
                assert any(c['purpose']==('pandoc_compile' if handler=='report' else 'latex_compile') for c in children.values())
                assert [json.loads(s) for s in (api.root/'actual-root-releases.jsonl').read_text().splitlines()]==releases
                assert sum(e['type']=='jobs.updated' and e['payload'].get('status')=='succeeded' for e in final['terminal_events'])==1
                assert request['result']['usage']['observed_output_bytes']>=captured['output']['output_preparation']['output_usage']['observed_output_bytes']
                result=call('GET',api.prefix+('/report/results' if handler=='report' else '/latex/results'))
                assert result['publication']['id']==submitted['publication_id'] and result['publication']['current'] and not result['publication']['stale']
                if handler=='report':
                    html=api.call('GET',api.prefix+'/raw?path=E-report/build/report.html',raw=True)
                    docx=api.call('GET',api.prefix+'/raw?path=E-report/build/report.docx',raw=True)
                    assert b'Original recovery document' in html
                    with zipfile.ZipFile(io.BytesIO(docx)) as z:assert b'Original recovery document' in z.read('word/document.xml')
                else:
                    from app.services.latex_artifacts import paths
                    pdf=api.call('GET',api.prefix+'/raw?path='+paths('E-report/latex/main.tex')['pdf'],raw=True)
                    from pypdf import PdfReader
                    assert 'Original recovery document' in ''.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
                report['checks'].append({'check':'actual_registered_document_archive_cold_cache_independent_compile_terminal_restart_and_original_publication','handler':handler,'original_root_releases':1,'actual_recovery_native_children':len(children),'terminal_boundary':boundary})
                if model_configuration is not None:
                    profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual recovered finalizer readback','base_url':model_configuration['base_url'],
                        'default_model':model_configuration['model'],'api_key':model_configuration['key']})
                    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Read actual recovered source','provider_profile_id':profile['id'],
                        'model':model_configuration['model'],'permission_mode':'plan'})
                    events=[]
                    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
                        with client.stream('POST','/conversations/'+str(conversation['id'])+'/messages',json={'content':'请仅调用一次 read_text_file 工具读取 E-report/report.md。拿到实际内容后，只原样返回其中以 RECOVERED_ 开头的完整验证字符串。不要猜测，不要修改文件，不要调用其他工具。',
                                'provider_profile_id':profile['id'],'model':model_configuration['model']}) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if line.startswith('data:'):events.append(json.loads(line[5:]))
                    upstream=[json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
                    calls=[v for v in upstream if v['kind']=='http' and v['path'].endswith('/chat/completions')];usage=[v for v in upstream if v.get('usage')]
                    report['model_calls']=len(calls);report['actual_model_readback']={'provider':'deepseek','host':'api.deepseek.com','model':model_configuration['model'],
                        'HTTP_statuses':[v['status'] for v in calls],'usage_receipts':len(usage),'reported_tokens':sum(v['usage']['total_tokens'] for v in usage),'upstream':upstream,
                        'answer':''.join(e.get('text','') for e in events if e.get('type')=='text.delta'),'nonce':model_nonce}
                    raw=json.dumps(events,ensure_ascii=False,indent=2);assert model_configuration['key'] not in raw
                    (api.root/'actual-recovered-model-events.json').write_text(raw+'\n',encoding='utf8');save()
                    assert len(calls)==len(usage)==2 and all(v['status']==200 and v['host']=='api.deepseek.com' for v in calls)
                    assert not [e for e in events if e.get('type') in {'stream.error','request.failed','request.timed_out'}]
                    tools=[e for e in events if e.get('type')=='tool.result'];assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok']
                    assert model_nonce in report['actual_model_readback']['answer'] and any(e.get('type')=='run.completed' for e in events)
                    report['checks'].append({'check':'actual_DeepSeek_reads_original_recovered_source_nonce_through_real_file_tool','calls':2,'mock_llm_calls':0})
                report['functional_passed']=True
    except CounterexampleComplete:pass
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1500],'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if disconnected_socket is not None:disconnected_socket.close()
        report['API_exit_probes']=[]
        for birth in report['API_births']:
            try:observed=exited(birth)
            except BaseException as error:
                observed=identity.probe(birth)
                report.setdefault('API_exit_probe_failures',[]).append({'birth':birth,'type':type(error).__name__,'actual_current_probe':observed})
            report['API_exit_probes'].append(observed)
        for handle in api_handles.values():handle.close()
        if report.get('actual_validator_crash'):
            birth=report['actual_validator_crash']['birth'];until=time.monotonic()+70
            if birth['platform']=='linux':
                inflight=report['validation_inflight']['finalization_requests'][0]
                birth=next(c['identity']['native']['process'] for c in inflight['children']['children'].values() if c['phase']=='running' and c['purpose']=='latex_compile')
                report['exit_probe_scope']='Observed outer owned native gate; the held helper birth is inside its private PID namespace. Full native tree proof is checked separately through kernel retirement.'
            while True:
                if birth['platform']=='linux':
                    from app.services.owned_process import probe_native
                    probe=asyncio.run(probe_native('vibeflow-linux',birth))
                else:probe=identity.probe(birth)
                if probe['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.1)
            report['actual_helper_exit_probe']=probe
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        report['passed']=bool(report.get('functional_passed') and report['store_stopped'] and not report.get('API_exit_probe_failures')
            and all(v['state']=='exited' for v in report['API_exit_probes']) and report.get('actual_helper_exit_probe',{'state':'exited'})['state']=='exited');save()
    print('PROFESSIONAL_FINALIZING_RESULT '+str(api.root/'professional-result.json'),flush=True)
    return report['passed'],api.root


async def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--handler',choices=['report','latex'],required=True);parser.add_argument('--terminal-boundary',choices=['before','after'],required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--interrupt-validation',action='store_true');parser.add_argument('--before-validation-fix',action='store_true');parser.add_argument('--preparation-boundary',choices=['request','reserved','plan','prepared','allocated','guardian','admitted','created','host_ready','native_ready']);parser.add_argument('--before-preparation-fix',action='store_true');parser.add_argument('--trace-transactions',action='store_true')
    parser.add_argument('--disconnect-validation',action='store_true');parser.add_argument('--before-cancellation-fix',action='store_true');parser.add_argument('--repeat-disconnect',type=int,choices=[0,1,2],default=0);parser.add_argument('--mixed-error',action='store_true');parser.add_argument('--before-mixed-fix',action='store_true');parser.add_argument('--source-profile-id',type=int);parser.add_argument('--cancel-original-job',action='store_true');parser.add_argument('--before-job-cancel-fix',action='store_true');parser.add_argument('--two-apis',action='store_true');parser.add_argument('--io-boundary',choices=['cache_prepared','log_admitted','log_put_returned']);parser.add_argument('--cancel-before-finalize',action='store_true');parser.add_argument('--legacy-output-protocol',action='store_true');parser.add_argument('--inspect-pending-archive',action='store_true');parser.add_argument('--pending-archive-cancel',choices=['none','drain','exit','unadmitted']);parser.add_argument('--budget-recovery',choices=['diagnostic','expired','log_limit']);args=parser.parse_args(argv)
    if args.budget_recovery and (args.handler!='report' or (not args.interrupt_validation and not args.pending_archive_cancel) or (args.pending_archive_cancel and args.budget_recovery!='expired') or args.disconnect_validation or args.preparation_boundary or args.cancel_original_job or args.io_boundary or args.mixed_error or args.source_profile_id is not None):parser.error('Budget recovery requires actual report validator-owner exit, or expired pending-archive cancellation')
    if args.pending_archive_cancel and (args.inspect_pending_archive or args.cancel_original_job or args.cancel_before_finalize or args.io_boundary or args.interrupt_validation or args.preparation_boundary or args.disconnect_validation or args.mixed_error or args.legacy_output_protocol or args.source_profile_id is not None):parser.error('Pending archive cancellation requires its own actual copy boundary')
    if args.inspect_pending_archive and (args.cancel_original_job or args.cancel_before_finalize or args.io_boundary or args.interrupt_validation or args.preparation_boundary or args.disconnect_validation or args.mixed_error or args.legacy_output_protocol):parser.error('Pending archive diagnosis requires its own actual cancellation scenario')
    if args.legacy_output_protocol and (not args.cancel_before_finalize or args.before_job_cancel_fix):parser.error('Legacy protocol check requires fixed before-first-finalize cancellation')
    if args.io_boundary and (args.preparation_boundary or args.interrupt_validation or args.mixed_error or args.disconnect_validation):parser.error('IO interruption requires its own actual operation boundary')
    if args.cancel_before_finalize and (not args.cancel_original_job or args.interrupt_validation or args.preparation_boundary or args.io_boundary or args.disconnect_validation or args.mixed_error):parser.error('Before-first-finalize cancellation requires its own actual original Job cancellation scenario')
    if args.cancel_original_job and (args.disconnect_validation or args.mixed_error or not (args.interrupt_validation or args.preparation_boundary or args.io_boundary or args.cancel_before_finalize)):parser.error('Original Job cancellation requires its own actual operation boundary')
    if args.before_job_cancel_fix and not args.cancel_original_job:parser.error('Job cancel counterexample requires actual Job cancellation')
    if args.two_apis and (args.before_job_cancel_fix or args.before_cancellation_fix or args.before_mixed_fix or args.before_validation_fix or args.before_preparation_fix):parser.error('Cross-API competition verifies the current fixed path')
    if args.source_profile_id is not None and __name__=='__main__':parser.error('Use the explicit live_llm_mixed_finalizer_acceptance.py entry for actual model acceptance')
    if args.source_profile_id is not None and (not args.mixed_error or args.handler!='report' or args.before_mixed_fix):parser.error('Actual model readback requires a fixed mixed report scenario')
    if args.disconnect_validation and not (args.interrupt_validation or args.preparation_boundary or args.mixed_error):parser.error('Disconnect requires actual running or preparation boundary')
    if args.mixed_error and (not args.disconnect_validation or args.interrupt_validation or args.preparation_boundary or args.repeat_disconnect):parser.error('Mixed OS error requires its own actual cleanup disconnect boundary')
    if args.before_mixed_fix and not args.mixed_error:parser.error('Mixed counterexample requires actual OS error and disconnect')
    if args.before_cancellation_fix and not args.disconnect_validation:parser.error('Cancellation counterexample requires real HTTP disconnect')
    if args.repeat_disconnect and (not args.disconnect_validation or args.before_cancellation_fix):parser.error('Repeated disconnect requires fixed cancellation path')
    if args.before_validation_fix and not args.interrupt_validation:parser.error('Before-fix counterexample requires actual validation interruption')
    if args.before_preparation_fix and not args.preparation_boundary:parser.error('Preparation counterexample requires a real preparation boundary')
    if args.preparation_boundary and args.interrupt_validation:parser.error('Choose one real interruption boundary')
    async def run(url=None):return await asyncio.to_thread(verify,args.handler,args.terminal_boundary,database_url=url,interrupt_validation=args.interrupt_validation,before_fix=args.before_validation_fix or args.before_preparation_fix or args.before_cancellation_fix or args.before_mixed_fix or args.before_job_cancel_fix,preparation_boundary=args.preparation_boundary,trace_transactions=args.trace_transactions,disconnect_validation=args.disconnect_validation,repeat_disconnect=args.repeat_disconnect,mixed_error=args.mixed_error,source_profile_id=args.source_profile_id,cancel_original_job=args.cancel_original_job,two_apis=args.two_apis,io_boundary=args.io_boundary,cancel_before_finalize=args.cancel_before_finalize,legacy_output_protocol=args.legacy_output_protocol,inspect_pending_archive=args.inspect_pending_archive,pending_archive_cancel=args.pending_archive_cancel,budget_recovery=args.budget_recovery)
    if not args.postgres:return 0 if (await run())[0] else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url);assert selected.drivername=='postgresql+asyncpg' and selected.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='professional-finalizing-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
    print('PROFESSIONAL_FINALIZING_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');passed,api=await run(url);report.update(passed=passed,api_root=str(api));return 0 if passed else 1
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
