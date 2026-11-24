"""Actual queue, committed reservation and native preparation deadline cases."""
import asyncio,json,socket,subprocess,time
from pathlib import Path
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,sandbox_image_launch as launch,process_identity_native as identity
from app.services.owned_process import probe_native
from sandbox_image_preparation_peer_checks import FIXTURE as PREPARATION_FIXTURE

STAGES=('queue','reserve_return','allocated','host_transport_return','native_ready','pre_release')
FIXTURE=PREPARATION_FIXTURE+'''
from app.services.native_request_execution import RequestExecutor as _DeadlineExecutor
from app.services import native_execution_client as _native_client,native_request_records as _records
_deadline_operations={}
def _deadline_case():
 path=Path('deadline-case.json')
 return json.loads(path.read_text()) if path.exists() else None
_deadline_command=_DeadlineExecutor._command
def _bounded_command(self,*args,**kwargs):
 command,timeout,limit=_deadline_command(self,*args,**kwargs)
 selected=_deadline_case()
 return (command,selected['seconds'],limit) if self.kind=='terminal.cleanup' and selected else (command,timeout,limit)
_DeadlineExecutor._command=_bounded_command
_deadline_client=_native_client.run_native
def _bounded_client(*args,**kwargs):
 selected=_deadline_case()
 if selected and args[0]=='sandbox_image_cleanup':kwargs['timeout']=selected['seconds']
 return _deadline_client(*args,**kwargs)
_native_client.run_native=_bounded_client
_deadline_reserve=_records.reserve
async def _reserve_delayed(owner,nonce,identifier,*args,**kwargs):
 result=await _deadline_reserve(owner,nonce,identifier,*args,**kwargs)
 await _pause_preparation('reserve_return',owner,identifier)
 return result
_records.reserve=_reserve_delayed
_deadline_execute=_DeadlineExecutor._execute
async def _execute_deadline(self,entry,*args,**kwargs):
 selected=_deadline_case()
 if self.kind!='terminal.cleanup' or not selected:return await _deadline_execute(self,entry,*args,**kwargs)
 _deadline_operations[self.owner.id]=asyncio.current_task()
 slots=self.slots;before_release=self.before_release
 Path('deadline-entry.json').write_text(json.dumps({'request_id':self.owner.id,'child_id':entry.id,'deadline':entry.deadline}))
 if selected['stage']=='queue':
  self.slots=asyncio.Semaphore(0)
  pending=Path('preparation-paused.pending')
  pending.write_text(json.dumps({'stage':'queue','request_id':self.owner.id,'child_id':entry.id}))
  pending.replace('preparation-paused.json')
 if selected['stage']=='pre_release':
  async def delayed_release():
   if before_release is not None:await before_release()
   await _pause_preparation('pre_release',self.owner,entry.id)
  self.before_release=delayed_release
 failure=None
 try:return await _deadline_execute(self,entry,*args,**kwargs)
 except BaseException as error:failure=type(error).__name__;raise
 finally:
  self.slots=slots;self.before_release=before_release
  _deadline_operations.pop(self.owner.id,None)
  Path('deadline-finished.json').write_text(json.dumps({'request_id':self.owner.id,'reason':entry.reason,'error':failure}))
_DeadlineExecutor._execute=_execute_deadline
@app.post('/fixture/native-operation-cancel/{request_id}')
async def interrupt_deadline(request_id:str,user=Depends(get_current_user)):
 from app.models import NativeRequest
 async with SessionLocal() as db:
  row=await db.get(NativeRequest,request_id)
  if row is None or row.created_by!=user.id:raise HTTPException(404)
 task=_deadline_operations.get(request_id)
 if task is None:raise HTTPException(409)
 task.cancel()
 return {'still_running':not task.done()}
'''

async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'cases':[],'checks':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    worker=None;first=None;log=(api.root/'deadline-peer-api.log').open('w',encoding='utf8')
    async def state(identifier):
        async with sessions() as db:
            row=await db.get(NativeRequest,identifier);children=list(scopes.records.document(row)['children'].values())
            child=children[0] if children else None
            return {'phase':row.phase,'error':row.error,'children_json':row.children_json,'result_json':row.result_json,
                'owner':json.loads(row.owner_identity_json),'child':child,'scope':await scopes.evidence(db,row,child) if child else None}
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('NATIVE_DEADLINE_PASS '+name,flush=True)
    try:
        async with sessions() as db:selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip';retained=archive.read_bytes()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=list(api.proc.args);args[args.index('--port')+1]=str(port)
        worker=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        peer='http://127.0.0.1:'+str(port);report['peer']=peer
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=180)) as client:
            async with asyncio.timeout(40):
                while True:
                    assert worker.poll() is None
                    try:
                        async with client.get(peer+'/api/health') as response:
                            if response.status==200:break
                    except aiohttp.ClientError:pass
                    await asyncio.sleep(.1)
            endpoint='/api/v1/terminal/sessions/'+sid+'/image-cleanup'
            async def submit(host):
                async with client.post(host+endpoint,json={'recovery_id':rid,'confirm':True}) as response:return response.status,await response.text()
            for index,stage in enumerate(STAGES):
                for name in ('resume-preparation','preparation-paused.json','deadline-entry.json','deadline-finished.json'):(api.root/name).unlink(missing_ok=True)
                seconds=8 if stage in {'queue','reserve_return'} else 20
                (api.root/'deadline-case.json').write_text(json.dumps({'stage':stage,'seconds':seconds}))
                (api.root/'pause-preparation.json').write_text(json.dumps({'stage':stage}))
                owner_host,other=(api.base,peer) if index%2==0 else (peer,api.base)
                first=asyncio.create_task(submit(owner_host))
                async with asyncio.timeout(40):
                    while not (api.root/'preparation-paused.json').exists():
                        assert not first.done(),await first if first.done() else 'Expected actual deadline checkpoint'
                        await asyncio.sleep(.02)
                marker=json.loads((api.root/'preparation-paused.json').read_text());entry=json.loads((api.root/'deadline-entry.json').read_text())
                before=await state(marker['request_id']);assert before['phase']=='active'
                assert identity.probe(before['owner'])['state']=='match' and (await submit(other))[0]==409
                if stage=='queue':assert before['child'] is None
                if stage=='reserve_return':assert before['child']['phase']=='preparing' and before['scope'] is None
                await asyncio.sleep(max(0,entry['deadline']-time.monotonic()+.3))
                if stage!='queue':
                    assert not first.done() and (await state(marker['request_id']))['phase']=='active'
                    assert (await submit(other))[0]==409
                    if stage=='reserve_return':
                        for _ in range(3):
                            async with client.post(owner_host+'/fixture/native-operation-cancel/'+marker['request_id']) as response:
                                assert response.status==200 and (await response.json())['still_running']
                    (api.root/'resume-preparation').write_text('drain after shared absolute deadline')
                code,raw=await first;assert code==409,(stage,code,raw)
                after=await state(marker['request_id']);finished=json.loads((api.root/'deadline-finished.json').read_text())
                case={'stage':stage,'seconds':seconds,'entry':entry,'before':before,'after':after,'execution':finished,'paused':marker}
                report['cases'].append(case)
                assert after['phase']=='failed' and finished['reason']=='timeout'
                if stage=='queue':assert after['child'] is None
                else:assert after['child']['phase']=='failed'
                if stage=='reserve_return':
                    assert after['scope'] is None and after['child']['ended_at'] is not None
                    assert finished['error']=='CancelledError'
                    async with sessions() as db:
                        row=await db.get(NativeRequest,marker['request_id'])
                        sealed=await launch.unstarted_evidence(db,row,after['child']);assert sealed is not None
                        case['unstarted']=sealed
                        event=await db.get(DurableEvent,launch.event_id(after['child']['id'],'unstarted'))
                        original=event.payload_json;corrupted=json.loads(original);corrupted['binding']['request_id']='another-request'
                        event.payload_json=json.dumps(corrupted);await db.commit()
                    try:assert (await submit(other))[0]==409
                    finally:
                        async with sessions() as db:
                            event=await db.get(DurableEvent,launch.event_id(after['child']['id'],'unstarted'))
                            event.payload_json=original;await db.commit()
                    record('changed_unstarted_binding_refuses_recovery','protocol_fault')
                if after['scope'] is not None:assert after['scope']['closed'] is not None
                if stage in {'allocated','host_transport_return'}:assert after['scope']['admitted'] is None
                exits={}
                if stage=='host_transport_return':exits['created_host']=identity.probe(marker['host'])
                if after['scope'] is not None and after['scope']['admitted'] is not None:
                    flight=after['scope']['admitted']['native_preflight']
                    exits.update(host=identity.probe(flight['host']),native=await probe_native(flight['native']['distribution'],flight['native']['process']))
                assert all(v['state']=='exited' for v in exits.values());case['actual_exits']=exits
                assert image.exists() and archive.read_bytes()==retained
                (api.root/'deadline-case.json').unlink();(api.root/'pause-preparation.json').unlink(missing_ok=True)
                record(stage+'_deadline_preserves_cleanup_without_late_release')
            code,raw=await submit(peer);assert code==200,(code,raw);done=json.loads(raw)
            assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
            for host in (api.base,peer):
                code,raw=await submit(host);assert code==200 and json.loads(raw)==done
                for mode in ('validation','admin'):
                    async with client.get(host+f'/fixture/{api.case_id}/{mode}') as response:assert response.status==200
            for case in report['cases']:
                final=await state(case['entry']['request_id'])
                assert final['children_json']==case['after']['children_json'] and final['result_json']==case['after']['result_json']
            async with sessions() as db:
                completed=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
                assert len(completed)==1
            assert archive.read_bytes()==retained;report['completion']=done
            record('both_apis_retry_once_after_deadline_drains')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            report['checks'].append({'name':'real_model_reads_restored_snapshot','kind':'real_llm'})
        report['passed']=True;return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};raise
    finally:
        (api.root/'resume-preparation').write_text('drain actual deadline preparation')
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        if worker is not None:
            if worker.poll() is None:worker.terminate()
            await asyncio.to_thread(worker.wait,10);report['peer_exited']=worker.returncode is not None
        log.close();await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'native-deadline-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')

async def run(profile_id,*,postgres=False):
    from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
    from live_llm_sandbox_recovery_acceptance import model_read
    from sandbox_retirement_checks import verify
    configuration=await source(profile_id);result={}
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def scenario(api,sid,rid,proof,continuation=None):
        result['fixture_root']=str(api.root)
        result['llm_calls']=await after_retirement(api,sid,rid,proof,continuation=continuation or model)
        return result['llm_calls']
    if postgres:
        from sandbox_image_postgres_checks import run as pg_run
        await pg_run(fixture_prefix=LIVE_FIXTURE,execution_fixture=FIXTURE,scenario=scenario,continuation=model)
    else:await verify(fixture_prefix=LIVE_FIXTURE+FIXTURE,after_retirement=scenario)
    return result
