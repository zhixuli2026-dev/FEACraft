"""Actual WSL/API death, retained snapshot and durable retirement; no LLM.

Fixture endpoints only invoke production admission checks under their case lock.
SQL corruption is explicitly reported as fault evidence, not process evidence.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import aiohttp
from sqlalchemy import select, update, inspect as inspect_schema
from sqlalchemy.ext.asyncio import create_async_engine

from testsupport import IsolatedServer
from sandbox_recovery_ui_cleanup import verify as verify_cleanup
from app.models import DurableEvent, NativeRequest, NativeSession

FIXTURE = '''
from fastapi import Depends, HTTPException
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.terminal import _authorize_terminal
from app.services.durable_events import lock_case_execution
from app.services.case_validation import require_sessions_stopped_locked
from app.services.case_admin import _no_terminal_sessions, CaseAdminError
from app.services.case_write import CaseWriteConflict
@app.get('/fixture/{case_id}/{mode}')
async def admission(case_id:int,mode:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  await lock_case_execution(db,case_id)
  case=await _authorize_terminal(db,case_id,user.id)
  try:
   if mode=='validation':await require_sessions_stopped_locked(db,case)
   elif mode=='admin':await _no_terminal_sessions(db,[case_id])
   else:raise HTTPException(404)
  except (CaseWriteConflict,CaseAdminError):raise HTTPException(409,'native records block admission')
  return {'admitted':True}
'''


async def verify(store=None, *, fixture_prefix='', after_retirement=None, database_url=None, before_recovery=None,
                 before_sandbox=None, sandbox_command=''):
    api=IsolatedServer('sandbox-recovery-ui-retirement',app='retirement_fixture:app',database_url=database_url)
    (api.root/'retirement_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+fixture_prefix+FIXTURE,encoding='utf8')
    if store:store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'ready.json').write_text(json.dumps({'root':str(api.root)}))
    report={'passed':False,'storage':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite',
            'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    engine=create_async_engine(database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        (api.root/'retirement-checks.json').write_text(json.dumps(report,indent=2))
        print('RETIREMENT_PASS '+name,flush=True)
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    async def gate(mode,expect):
        return await call('GET',f'/fixture/{api.case_id}/{mode}',api_prefix=False,expect=expect)
    async def rows():
        async with engine.connect() as db:
            terminal=dict((await db.execute(select(NativeSession.__table__).where(NativeSession.id==sid))).mappings().one())
            exports=[dict(row) for row in (await db.execute(select(NativeRequest.__table__).where(
                NativeRequest.case_id==api.case_id,NativeRequest.kind=='terminal.export').order_by(NativeRequest.id))).mappings()]
        return terminal,exports
    async def restart():
        args=api.proc.args
        api.proc.kill();await asyncio.to_thread(api.proc.wait,10)
        api.proc=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
                except OSError:pass
                await asyncio.sleep(.1)
    try:
        with api:
            api.create_case('Retire actual dead sandbox')
            if before_sandbox is not None:await before_sandbox(api)
            suffix=uuid.uuid4().hex;proof='RETIREMENT_'+suffix
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as client:
                async with client.ws_connect(api.base+'/ws/terminal',params={'token':api.token,'case':api.case_id,'shell':'wsl-sandbox'}) as ws:
                    ready=await ws.receive_json(timeout=90);assert ready['type']=='ready',ready
                    sid=ready['session'];endpoint='/terminal/sessions/'+sid+'/retirement'
                    assert (await call('GET',endpoint))['status']=='not_retired'
                    await ws.send_json({'type':'input','data':sandbox_command+"printf '%s%s\\n' 'RETIREMENT_' '"+suffix+"' > Ref/retirement.txt; cat Ref/retirement.txt\r"})
                    output=b''
                    async with asyncio.timeout(35):
                        while proof.encode() not in output:
                            message=await ws.receive();assert message.type==aiohttp.WSMsgType.BINARY,message
                            output+=message.data;assert len(output)<1024*1024
                    await gate('validation',409);await gate('admin',409)
                    record('actual_live_terminal_blocks_validation_and_admin')
                    await restart()
                async with asyncio.timeout(60):
                    while True:
                        observed=await call('GET','/terminal/sessions/'+sid+'/recovery-inspection')
                        if observed['status']=='processes_exited':break
                        await asyncio.sleep(.2)
                if before_recovery is not None:await before_recovery(api,sid,proof)
                async with client.post(api.base+'/api/v1/terminal/sessions/'+sid+'/recoveries',headers={'Authorization':'Bearer '+api.token}) as response:
                    recovery=await response.json();assert response.status==201,(response.status,recovery)
            rid=recovery['id'];body={'recovery_id':rid,'confirm':True}
            original,exports=await rows()
            assert exports,'Actual original exporter must be registered, not a synthetic SQL record'
            assert original['exit_code'] is None
            await gate('validation',409);await gate('admin',409)
            record('actual_api_death_and_completed_snapshot_do_not_alone_release_original_records')
            await call('POST',endpoint,body={'recovery_id':rid},expect=422)
            await call('POST',endpoint,body={**body,'confirm':False},expect=422)
            stranger=await call('POST','/auth/register',body={'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other'})
            await call('POST',endpoint,body=body,extra_headers={'Authorization':'Bearer '+stranger['token']},expect=404)
            await call('GET',endpoint,extra_headers={'Authorization':'Bearer '+stranger['token']},expect=404)
            record('confirmation_and_actual_owner_authorization_required','protocol_fault')
            async with engine.connect() as db:
                profile=await db.scalar(select(NativeRequest.profile_json).where(NativeRequest.id==rid))
            archive=Path(json.loads(profile)['root'])/'recovery.zip'
            raw=archive.read_bytes()
            try:
                archive.write_bytes(raw[:-1])
                await call('POST',endpoint,body=body,expect=409)
                assert (await rows())== (original,exports)
            finally:archive.write_bytes(raw)
            record('actual_corrupt_archive_refused_without_fencing_original_rows','protocol_fault')
            retired=await call('POST',endpoint,body=body)
            assert retired['status']=='retired' and retired['process_exit_verified'] is True
            assert retired['original_exit_code'] is None and retired['normal_stop_receipt_invented'] is False
            assert retired['retired_exports']==len(exports)
            terminal,updated=await rows()
            for old,new in [(original,terminal),*zip(exports,updated)]:
                assert new['phase']=='failed' and new['version']==old['version']+1
                assert new['retirement_event_id']==retired['event_id']
                for key in ('process_json','stop_json','exit_code','children_json','profile_json','owner_identity_json','result_json'):
                    if key in old:assert old[key]==new[key],key
            state=await call('GET','/terminal/sessions/'+sid)
            assert state['cleanup_verified'] is not True and state['exit_code'] is None
            assert (await gate('validation',200))['admitted']
            await gate('admin',409 if store else 200)
            record('real_exit_observations_fence_original_terminal_and_export_without_forged_stop')
            record('production_validation_admits_retirement_while_retained_s3_staging_still_blocks_admin')
            assert await call('GET',endpoint)==retired
            assert await call('POST',endpoint,body=body)==retired
            await call('POST',endpoint,body={**body,'recovery_id':str(uuid.uuid4())},expect=409)
            assert (await rows())==(terminal,updated)
            record('repeat_confirmation_is_idempotent_and_other_snapshot_conflicts')
            async with engine.connect() as db:
                witness=await db.scalar(select(DurableEvent.payload_json).where(DurableEvent.id==retired['event_id']))
            try:
                corrupted=json.loads(witness);corrupted['observations'][0]['observation']['state']='unverified'
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id==retired['event_id']).values(payload_json=json.dumps(corrupted)))
                await call('GET',endpoint,expect=409);await gate('validation',409);await gate('admin',409)
            finally:
                async with engine.begin() as db:
                    await db.execute(update(DurableEvent).where(DurableEvent.id==retired['event_id']).values(payload_json=witness))
            try:
                async with engine.begin() as db:
                    await db.execute(update(NativeSession).where(NativeSession.id==sid).values(exit_code=0))
                await call('GET',endpoint,expect=409);await gate('validation',409)
            finally:
                async with engine.begin() as db:
                    await db.execute(update(NativeSession).where(NativeSession.id==sid).values(exit_code=None))
            record('corrupted_exit_observation_or_invented_exit_code_revokes_admission','protocol_fault')
            await restart()
            assert await call('GET',endpoint)==retired
            assert await call('POST',endpoint,body=body)==retired
            await gate('validation',200)
            async with engine.connect() as db:
                events=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.sandbox_retired',DurableEvent.case_id==api.case_id)))
            assert events==[retired['event_id']]
            await call('GET',api.prefix+'/raw?path=Ref/retirement.txt',expect=404)
            assert archive.read_bytes()==raw
            record('actual_second_restart_retains_one_witness_archive_and_unmodified_project')
            if after_retirement is not None:
                report['llm_calls']=await after_retirement(api,sid,rid,proof)
                if report['llm_calls']:
                    record('real_model_reads_restored_shell_output_after_retirement','real_llm')
            report.update(passed=True,session_id=sid,recovery_id=rid,event_id=retired['event_id'])
    finally:
        try:
            if database_url:
                async with engine.connect() as db:
                    tables=await db.run_sync(lambda connection:set(inspect_schema(connection).get_table_names()))
                    if {'native_sessions','native_requests'} <= tables:
                        journals={'sessions':[tuple(row) for row in (await db.execute(select(NativeSession.profile_json,NativeSession.process_json)))],
                                  'requests':[tuple(row) for row in (await db.execute(select(NativeRequest.children_json)))]}
                    else:journals=None
                if journals is not None:
                    (api.root/'native-journals.json').write_text(json.dumps(journals,indent=2))
                    await verify_cleanup(api.root,journals=journals)
                else:
                    (api.root/'cleanup.json').write_text(json.dumps({'status':'schema_not_initialized','native_journals_unavailable':True}))
            elif (api.root/'test.sqlite').exists():
                await verify_cleanup(api.root)
        finally:await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():
            upstream=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in upstream)
        (api.root/'retirement-checks.json').write_text(json.dumps(report,indent=2))
    print('RETIREMENT_VERIFIED '+str(api.root),flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    if args.s3:
        from s3testsupport import ObjectStore
        runtime=Path(__file__).resolve().parents[2]/'third_party/seaweed-4.52-runtime/weed'
        with ObjectStore(executable=runtime) as store:asyncio.run(verify(store))
    else:asyncio.run(verify())
