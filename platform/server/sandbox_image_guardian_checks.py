"""Actual API exits around Windows host creation, plus corrupted proof refusal."""
import asyncio
import json
import os
from pathlib import Path
import subprocess

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import NativeRequest, DurableEvent
from app.services import sandbox_image_scope as scopes, sandbox_image_launch as launch
from app.services import process_identity_native as identity, windows_scope_guardian as guardian
from app.services.owned_process import probe_native

FIXTURE = '''
import os,json
from pathlib import Path
from app.services import sandbox_image_scope as _gs, sandbox_image_launch as _gl
from app.services import windows_process_create as _gc, process_identity_native as _gi
from app.services.windows_process_scope import ProcessHandle as _GHandle
_guardian_selection = None
_guardian_prepare = _gs.prepare
async def _prepared_guardian(owner,child_id,process,distro,**kwargs):
 global _guardian_selection
 await _guardian_prepare(owner,child_id,process,distro,**kwargs)
 _guardian_selection = {'request_id':owner.id,'child_id':child_id,'nonce':process.nonce,
  'guardian':{'request':process.guardian.request,'preparation':process.guardian.prepared}}
_gs.prepare = _prepared_guardian
def _guardian_fault(stage, birth=None):
 path = Path('guardian-startup-fault.json')
 if path.exists() and json.loads(path.read_text())['stage'] == stage:
  path.unlink()
  with Path('guardian-startup-deaths.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps(dict(_guardian_selection,stage=stage,owner=_gi.observe(os.getpid()),business_birth=birth))+'\\n')
   stream.flush();os.fsync(stream.fileno())
  os._exit(81)
_guardian_admit = _gs.admit
async def _guarded_admit(owner,child_id,**kwargs):
 global _guardian_selection
 await _guardian_admit(owner,child_id,**kwargs)
 _guardian_selection = {'request_id':owner.id,'child_id':child_id,'nonce':kwargs['guardian']['request']['nonce'],
  'guardian':kwargs['guardian']}
 _guardian_fault('admitted')
_gs.admit = _guarded_admit
_guardian_create = _gc._create_process
def _guarded_create(args,scope,*rest,**kwargs):
 result = _guardian_create(args,scope,*rest,**kwargs)
 if _guardian_selection and scope.name == 'Local\\\\VibeFlow-'+_guardian_selection['nonce']:
  process = _GHandle(result[0])
  try: birth = process.identity(allow_exited=True)
  finally: process.close()
  _guardian_fault('created_before_adoption',birth)
 return result
_gc._create_process = _guarded_create
_guardian_observed = _gl.observed
async def _guarded_observed(owner,child_id,value):
 if value['stage'] == 'created':
  _guardian_fault('created_before_commit',value['host'])
 await _guardian_observed(owner,child_id,value)
_gl.observed = _guarded_observed
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report = {'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[],'crashes':[],
              'database':'postgresql' if api.database_url else 'sqlite'}
    engine = create_async_engine(api.database_url or 'sqlite+aiosqlite:///' + (api.root/'test.sqlite').as_posix())
    sessions = async_sessionmaker(engine,expire_on_commit=False)
    endpoint = '/terminal/sessions/'+sid+'/image-cleanup'
    body = {'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):
        return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        print('IMAGE_GUARDIAN_PASS '+name,flush=True)
    async def state(identifier):
        async with sessions() as db:
            row = await db.get(NativeRequest,identifier)
            child = next(iter(scopes.records.document(row)['children'].values()))
            return row.phase,child,await scopes.evidence(db,row,child),await launch.evidence(db,row,child)
    async def restart():
        api.proc = subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
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
            selected = json.loads((await db.get(NativeRequest,rid)).profile_json)
        image = Path(selected['image_root'])/'volume.ext4'
        archive = Path(selected['root'])/'recovery.zip'
        retained = archive.read_bytes()
        for stage in ('admitted','created_before_adoption','created_before_commit'):
            (api.root/'guardian-startup-fault.json').write_text(json.dumps({'stage':stage}))
            try:
                await call('POST',endpoint,body=body)
                raise AssertionError('API did not actually exit at the armed creation boundary')
            except (OSError,ConnectionError):pass
            assert await asyncio.to_thread(api.proc.wait,15) == 81
            death = json.loads((api.root/'guardian-startup-deaths.jsonl').read_text(encoding='utf8').splitlines()[-1])
            assert death['stage'] == stage
            phase,child,value,startup = await state(death['request_id'])
            assert phase=='active' and child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            assert not startup[1] and value['closed'] is None
            assert (value['admitted'] is None)==(stage=='created_before_adoption')
            chosen = death['guardian']
            if value['admitted'] is not None:assert chosen==value['admitted']['guardian']
            async with asyncio.timeout(20):
                while identity.probe(chosen['preparation']['guardian'])['state'] != 'exited':
                    await asyncio.sleep(.05)
            assert identity.probe(death['owner'])['state']=='exited'
            receipt = guardian.read(chosen['request'],'receipt.json')
            guardian.validate_receipt(chosen['request'],chosen['preparation'],receipt)
            assert receipt['reason'] in {'owner_exited','owner_pipe_closed'}
            assert receipt['membership']['total']>=1
            if death['business_birth'] is not None:
                assert identity.probe(death['business_birth'])['state']=='exited'
            if value['admitted'] is not None:
                native=value['admitted']['native_preflight']['native']
                assert (await probe_native(native['distribution'],native['process']))['state']=='exited'
            death['guardian_receipt']=receipt
            death['guardian_exit']=identity.probe(chosen['preparation']['guardian'])
            report['crashes'].append(death)
            await restart()
            assert (await call('GET',endpoint))['status']=='pending'
            assert (await state(death['request_id']))[2]['closed'] is None
            assert image.exists() and archive.read_bytes()==retained
            record('actual_API_exit_'+stage+'_has_no_child_registration_and_complete_guardian_exit')
            if stage=='admitted':
                path = Path(chosen['request']['root']['path'])/'receipt.json'
                original = path.read_bytes()
                try:
                    path.write_bytes(b'{')
                    await call('POST',endpoint,body=body,expect=409)
                    assert (await state(death['request_id']))[2]['closed'] is None and image.exists()
                    record('truncated_actual_guardian_receipt_blocks_retry_without_closure','protocol_fault')
                finally:path.write_bytes(original)
        done = await call('POST',endpoint,body=body)
        assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
        histories = {}
        for death in report['crashes']:
            phase,child,value,startup = await state(death['request_id'])
            assert phase=='failed' and child['phase']=='preparing' and child['identity'] is None and child['stop'] is None
            assert not startup[1]
            if death['stage']=='created_before_adoption':
                assert value['admitted'] is None and value['closed']['guardian'] is None
            else:assert value['closed']['guardian']['receipt']==death['guardian_receipt']
            assert value['closed']['receipt']['result']['state']=='absent'
            histories[death['stage']]=value
        report['histories']=histories
        async with sessions() as db:
            witnesses = list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.type=='terminal.image_cleanup_abandoned')))
            assert len(witnesses)==3
            assert all(json.loads(raw)['normal_stop_receipt_invented'] is False for raw in witnesses)
        record('three_actual_bootstrap_creation_crashes_reconcile_without_invented_child_registration_or_stop')
        event_id = scopes.event_id(report['crashes'][0]['child_id'],'closed')
        async with sessions() as db:
            original = (await db.get(DurableEvent,event_id)).payload_json
        changed = json.loads(original)
        changed['value']['guardian']['receipt']['membership']['pending']=1
        try:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=json.dumps(changed)))
            await call('GET',endpoint,expect=409)
            await call('POST',endpoint,body=body,expect=409)
            await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
            record('corrupt_persisted_guardian_membership_revokes_all_completion_gates','protocol_fault')
        finally:
            async with engine.begin() as db:
                await db.execute(update(DurableEvent).where(DurableEvent.id==event_id).values(payload_json=original))
        assert await call('GET',endpoint)==done and archive.read_bytes()==retained
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_retained_snapshot_after_guardian_recovery','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(item['kind']=='http' and item['path'].endswith('/chat/completions')
                                               for item in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'image-guardian-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
