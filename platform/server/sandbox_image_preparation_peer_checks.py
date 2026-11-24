"""Actual peer APIs, repeated owner cancellation and delayed preparation callbacks."""
import asyncio,json,os,socket,subprocess
from pathlib import Path
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,sandbox_image_launch as launch,process_identity_native as identity
from app.services.owned_process import probe_native
from app.services.native_scope_control import control
from sandbox_image_concurrency_checks import FIXTURE as CANCELLATION_FIXTURE

STAGES=('planned','allocation_prepared','allocation_permit','allocated','host_transport_return',
        'admitted','host_ready','native_handshake','native_ready')
FIXTURE=CANCELLATION_FIXTURE+'''
from app.services import sandbox_image_scope as _ps,sandbox_image_launch as _pl
from app.services.owned_process import OwnedProcess as _PeerOwned
_allocation_candidates={}
async def _pause_preparation(stage,owner,child_id,**facts):
 armed=Path('pause-preparation.json')
 if not armed.exists() or json.loads(armed.read_text())['stage']!=stage:return
 armed.unlink()
 pending=Path('preparation-paused.pending')
 pending.write_text(json.dumps({'stage':stage,'request_id':owner.id,'child_id':child_id,**facts}))
 pending.replace('preparation-paused.json')
 async with asyncio.timeout(120):
  while not Path('resume-preparation').exists():await asyncio.sleep(.02)
_peer_plan=_ps.plan
async def _plan_peer(owner,child_id,*args,**kwargs):
 result=await _peer_plan(owner,child_id,*args,**kwargs)
 await _pause_preparation('planned',owner,child_id)
 return result
_ps.plan=_plan_peer
_peer_allocated=_ps.allocated
async def _allocated_peer(owner,child_id,*args,**kwargs):
 result=await _peer_allocated(owner,child_id,*args,**kwargs)
 await _pause_preparation('allocated',owner,child_id)
 return result
_ps.allocated=_allocated_peer
_peer_reserve=_pl.reserve
async def _reserve_peer(owner,child_id,process,**kwargs):
 result=await _peer_reserve(owner,child_id,process,**kwargs)
 selected=kwargs.get('scope_preparation')
 if selected is not None:
  _allocation_candidates[selected['request']['nonce']]=(owner,child_id)
  await _pause_preparation('allocation_prepared',owner,child_id)
 return result
_pl.reserve=_reserve_peer
_peer_verify=_PeerOwned.verify_registration
async def _verify_peer(self):
 result=await _peer_verify(self)
 if self.nonce in _allocation_candidates:
  await _pause_preparation('allocation_permit',*_allocation_candidates[self.nonce])
 return result
_PeerOwned.verify_registration=_verify_peer
_peer_admit=_ps.admit
async def _admitted_peer(owner,child_id,*args,**kwargs):
 result=await _peer_admit(owner,child_id,*args,**kwargs)
 await _pause_preparation('admitted',owner,child_id)
 return result
_ps.admit=_admitted_peer
_peer_observed=_pl.observed
async def _observed_peer(owner,child_id,value):
 result=await _peer_observed(owner,child_id,value)
 await _pause_preparation(value['stage'],owner,child_id)
 return result
_pl.observed=_observed_peer
from app.services import windows_process_create as _wc
from app.services.windows_process_scope import ProcessHandle as _PeerHandle
_business_scopes={}
_peer_prepare_scope=_ps.prepare
async def _prepare_scope_peer(owner,child_id,process,*args,**kwargs):
 result=await _peer_prepare_scope(owner,child_id,process,*args,**kwargs)
 _business_scopes[process.scope.name]=(owner,child_id)
 return result
_ps.prepare=_prepare_scope_peer
_peer_create=_wc.create
async def _create_peer(scope,*args,**kwargs):
 process=await _peer_create(scope,*args,**kwargs)
 if scope.name in _business_scopes:
  handle=_PeerHandle(int(process._transport.get_extra_info('subprocess')._handle))
  try:
   assert scope.contains(handle)
   await _pause_preparation('host_transport_return',*_business_scopes[scope.name],host=handle.identity())
  finally:handle.close()
 return process
_wc.create=_create_peer
_peer_hello=_PeerOwned.hello
async def _hello_peer(self,role):
 result=await _peer_hello(self,role)
 if role=='wsl' and self.native_preflight is not None and self.scope.name in _business_scopes:
  await _pause_preparation('native_handshake',*_business_scopes[self.scope.name],native=result)
 return result
_PeerOwned.hello=_hello_peer
'''

async def after_retirement(api,sid,rid,proof,continuation=None,stages=STAGES):
    report={'passed':False,'cases':[],'checks':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    worker=None;first=None;log=(api.root/'preparation-peer-api.log').open('w',encoding='utf8')
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('PREPARATION_PEER_PASS '+name,flush=True)
    async def state(identifier):
        async with sessions() as db:
            row=await db.get(NativeRequest,identifier);child=next(iter(scopes.records.document(row)['children'].values()))
            return {'phase':row.phase,'children_json':row.children_json,'result_json':row.result_json,
                'owner':json.loads(row.owner_identity_json),'child':child,'scope':await scopes.evidence(db,row,child),
                'launch':await launch.evidence(db,row,child)}
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
                async with client.post(host+endpoint,json={'recovery_id':rid,'confirm':True}) as response:
                    return response.status,await response.text()
            for index,stage in enumerate(stages):
                for filename in ('resume-preparation','preparation-paused.json'):(api.root/filename).unlink(missing_ok=True)
                (api.root/'pause-preparation.json').write_text(json.dumps({'stage':stage}))
                owner_host,other=(api.base,peer) if index%2==0 else (peer,api.base)
                first=asyncio.create_task(submit(owner_host))
                async with asyncio.timeout(60):
                    while not (api.root/'preparation-paused.json').exists():
                        assert not first.done(),await first if first.done() else 'Preparation ended before checkpoint'
                        await asyncio.sleep(.02)
                paused=json.loads((api.root/'preparation-paused.json').read_text());before=await state(paused['request_id'])
                assert before['phase']=='active' and identity.probe(before['owner'])['state']=='match'
                assert (await submit(other))[0]==409 and image.exists()
                if before['scope']['allocated'] is not None:
                    observed=await control(before['scope']['planned']['distribution'],'observe',{'scope':before['scope']['allocated']['result']})
                    assert observed['result']['state']==('populated' if stage in {'native_handshake','native_ready'} else 'empty')
                else:observed=None
                if stage=='host_transport_return':
                    assert identity.probe(paused['host'])['state']=='match'
                    assert before['scope']['admitted'] is None and not before['launch'][1]
                if stage=='native_handshake':
                    assert before['scope']['admitted']['native_preflight']['native']['process']==paused['native']
                    assert 'native_ready' not in [v['stage'] for v in before['launch'][1]]
                for _ in range(3):
                    async with client.post(owner_host+'/fixture/image-cancel/'+sid) as response:
                        assert response.status==200 and (await response.json())['still_running']
                assert not first.done() and (await state(paused['request_id']))['phase']=='active'
                assert (await submit(other))[0]==409 and image.exists() and archive.read_bytes()==retained
                (api.root/'resume-preparation').write_text('allow the delayed preparation callback to return')
                reply=(await asyncio.gather(first,return_exceptions=True))[0]
                assert isinstance(reply,aiohttp.ClientError) or reply[0]==500
                after=await state(paused['request_id']);value=after['scope']
                case={'stage':stage,'request_id':paused['request_id'],'before':before,'after':after,'scope_while_paused':observed,
                    'paused':paused}
                report['cases'].append(case)
                assert after['phase']=='cancelled' and value['closed'] is not None
                observations=[v['stage'] for v in after['launch'][1]]
                if stage=='planned':assert value['prepared'] is None,'Cancelled planning still started scope allocation'
                if stage in {'allocation_prepared','allocation_permit'}:
                    assert value['allocated'] is None,'Cancelled controller preparation still issued an allocation permit'
                if stage=='allocated':assert value['admitted'] is None,'Cancelled allocation still admitted business preparation'
                if stage=='host_transport_return':
                    assert value['admitted'] is None,'Cancelled Windows transport still started and admitted the native helper'
                if stage=='admitted':assert not observations,'Cancelled admission still advanced preparation'
                if stage=='host_ready':assert 'native_ready' not in observations,'Cancelled host preparation still sent the native permit'
                exits={}
                if stage=='host_transport_return':
                    exits['created_host']=identity.probe(paused['host'])
                    assert exits['created_host']['state']=='exited'
                if value.get('admitted') is not None:
                    preflight=value['admitted']['native_preflight']
                    exits.update({'host':identity.probe(preflight['host']),
                        'native':await probe_native(preflight['native']['distribution'],preflight['native']['process'])})
                    assert all(v['state']=='exited' for v in exits.values())
                case['actual_exits']=exits
                assert image.exists() and archive.read_bytes()==retained
                record(stage+'_peer_refuses_and_cancelled_preparation_does_not_advance')
            code,raw=await submit(peer);assert code==200,(code,raw);done=json.loads(raw)
            assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
            repeat=await asyncio.gather(submit(api.base),submit(peer))
            assert all(status==200 and json.loads(raw)==done for status,raw in repeat)
            for case in report['cases']:
                final=await state(case['request_id'])
                assert final['children_json']==case['after']['children_json'] and final['result_json']==case['after']['result_json']
            async with sessions() as db:
                completed=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
                abandoned=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_abandoned')))
                assert len(completed)==1 and len(abandoned)==sum(stage not in {'native_ready','native_handshake'} for stage in stages)
            assert archive.read_bytes()==retained
            for host in (api.base,peer):
                for mode in ('validation','admin'):
                    async with client.get(host+f'/fixture/{api.case_id}/{mode}') as response:assert response.status==200
            record('both_workers_retry_once_after_all_cancelled_preparations_drain')
            report['completion']=done
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_snapshot_after_preparation_peer_competition','real_llm')
        report['passed']=True;return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};raise
    finally:
        (api.root/'resume-preparation').write_text('drain actual owned preparation')
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        if worker is not None:
            if worker.poll() is None:worker.terminate()
            await asyncio.to_thread(worker.wait,10);report['peer_exited']=worker.returncode is not None
        log.close();await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions')
            for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'preparation-peer-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
