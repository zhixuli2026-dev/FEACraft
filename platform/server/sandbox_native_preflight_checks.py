"""Actual API exits across native same-process handoff; no simulated inference."""
import asyncio
import json
from pathlib import Path
import subprocess

from sqlalchemy import select,update
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,sandbox_image_launch as launch
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native

FIXTURE = '''
import os,json
from pathlib import Path
from app.services import sandbox_image_scope as _ns, sandbox_image_launch as _nl
from app.services.owned_process import OwnedProcess as _NativeOwned,probe_native as _native_probe
from app.services.process_identity_native import observe as _native_host_observe
from app.services.native_scope_control import control as _native_scope_observe
from app.db import SessionLocal as _NativeSession
from app.models import NativeRequest as _NativeRequest
_native_selected = None
async def _native_fault(stage):
 path = Path('native-preflight-fault.json')
 if not path.exists() or json.loads(path.read_text())['stage']!=stage:return
 path.unlink()
 async with _NativeSession() as db:
  row=await db.get(_NativeRequest,_native_selected['request_id'])
  child=_ns.records.document(row)['children'][_native_selected['child_id']]
  scope=await _ns.evidence(db,row,child)
 preflight=scope['admitted']['native_preflight']
 native=await _native_probe(preflight['native']['distribution'],preflight['native']['process'])
 assert native['state']=='match'
 observed=await _native_scope_observe(scope['planned']['distribution'],'observe',{'scope':scope['allocated']['result']})
 with Path('native-preflight-deaths.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps(dict(_native_selected,stage=stage,owner=_native_host_observe(os.getpid()),
   native_preflight=preflight,actual_native_before_death=native,actual_scope_before_death=observed))+'\\n')
  stream.flush();os.fsync(stream.fileno())
 os._exit(81)
_native_admit = _ns.admit
async def _native_admitted(owner,child_id,**kwargs):
 global _native_selected
 await _native_admit(owner,child_id,**kwargs)
 _native_selected={'request_id':owner.id,'child_id':child_id}
 await _native_fault('admitted')
_ns.admit=_native_admitted
_native_observed = _nl.observed
async def _observe_native(owner,child_id,value):
 if value['stage']=='native_ready':await _native_fault('before_native_ready')
 await _native_observed(owner,child_id,value)
 await _native_fault(value['stage'])
_nl.observed=_observe_native
_native_hello = _NativeOwned.hello
async def _receive_native(self,role):
 if role=='wsl' and self.native_preflight is not None:await _native_fault('handoff_sent')
 return await _native_hello(self,role)
_NativeOwned.hello=_receive_native
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'checks':[],'crashes':[],'llm_calls':0,'mock_llm_calls':0,
            'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    endpoint='/terminal/sessions/'+sid+'/image-cleanup'
    body={'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        print('NATIVE_PREFLIGHT_PASS '+name,flush=True)
    async def state(identifier):
        async with sessions() as db:
            row=await db.get(NativeRequest,identifier)
            child=next(iter(scopes.records.document(row)['children'].values()))
            return row.phase,child,await scopes.evidence(db,row,child),await launch.evidence(db,row,child)
    async def restart():
        api.proc=subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):return
                except OSError:pass
                await asyncio.sleep(.1)
    try:
        async with sessions() as db:
            selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4'
        archive=Path(selected['root'])/'recovery.zip'
        original=archive.read_bytes()
        stages={'admitted':None,'created':'created','host_ready':'host_ready','handoff_sent':'host_ready',
                'before_native_ready':'host_ready','native_ready':'native_ready'}
        for stage,last in stages.items():
            (api.root/'native-preflight-fault.json').write_text(json.dumps({'stage':stage}))
            try:
                await call('POST',endpoint,body=body)
                raise AssertionError('API did not exit at the selected native startup boundary')
            except (OSError,ConnectionError):pass
            assert await asyncio.to_thread(api.proc.wait,15)==81
            death=json.loads((api.root/'native-preflight-deaths.jsonl').read_text(encoding='utf8').splitlines()[-1])
            assert death['stage']==stage
            phase,child,value,startup=await state(death['request_id'])
            assert phase=='active' and child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            assert (startup[1][-1]['stage'] if startup[1] else None)==last
            preflight=value['admitted']['native_preflight']
            assert preflight==death['native_preflight'] and value['closed'] is None
            scope_state=death['actual_scope_before_death']['result']['state']
            if stage=='handoff_sent':assert scope_state in {'empty','populated'}
            else:assert scope_state==('empty' if stage in {'admitted','created','host_ready'} else 'populated')
            async with asyncio.timeout(45):
                while True:
                    exits={'owner':identity.probe(death['owner']),'host':identity.probe(preflight['host']),
                        'native':await probe_native(preflight['native']['distribution'],preflight['native']['process']),
                        'guardian':identity.probe(value['admitted']['guardian']['preparation']['guardian'])}
                    if all(item['state']=='exited' for item in exits.values()):break
                    await asyncio.sleep(.1)
            death['actual_exit_observations']=exits
            report['crashes'].append(death)
            await restart()
            assert (await call('GET',endpoint))['status']=='pending'
            assert (await state(death['request_id']))[2]['closed'] is None
            assert image.exists() and archive.read_bytes()==original
            record('actual_API_exit_'+stage+'_retains_exact_preflight_birth_and_observes_exit')
        done=await call('POST',endpoint,body=body)
        assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
        report['histories']={}
        for death in report['crashes']:
            phase,child,value,startup=await state(death['request_id'])
            assert phase=='failed' and child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            assert value['closed']['guardian']['receipt']['verified'] and value['closed']['receipt']['result']['state']=='absent'
            report['histories'][death['stage']]=value
        async with sessions() as db:
            raw=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.type=='terminal.image_cleanup_abandoned')))
            assert len(raw)==len(stages)
            for item in map(json.loads,raw):
                assert item['normal_stop_receipt_invented'] is False
                assert len(item['observations'])==6
                assert all(item['observation']['state']=='exited' for item in item['observations'])
            rows=list(await db.scalars(select(NativeRequest).where(NativeRequest.kind=='terminal.cleanup',NativeRequest.phase=='succeeded')))
            assert len(rows)==1
            child=next(iter(scopes.records.document(rows[0])['children'].values()))
            value=await scopes.evidence(db,rows[0],child)
            assert child['identity']['native']==value['admitted']['native_preflight']['native']
            report['successful_same_process_identity']={'registered':child['identity'],
                'admitted':value['admitted']['native_preflight'],'scope':value}
        record('all_six_startup_crashes_recover_and_successful_gate_retains_the_same_native_birth')
        selected=report['crashes'][2]
        event_id=scopes.event_id(selected['child_id'],'admitted')
        async with sessions() as db:original_event=(await db.get(DurableEvent,event_id)).payload_json
        changed=json.loads(original_event)
        changed['value']['native_preflight']['native']['process']['start_ticks']='1'
        try:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=json.dumps(changed)))
            await call('GET',endpoint,expect=409)
            await call('POST',endpoint,body=body,expect=409)
            await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
            record('changed_native_preflight_birth_revokes_completion_retry_and_case_admission','protocol_fault')
        finally:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=original_event))
        assert await call('GET',endpoint)==done and archive.read_bytes()==original
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_recovered_output_after_native_handoff_crashes','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(item['kind']=='http' and item['path'].endswith('/chat/completions')
                                               for item in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'native-preflight-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
