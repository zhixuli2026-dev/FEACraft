"""Native lifetime admission versus source reservations on two disposable APIs.

Real PTY boundary/stop evidence plus valid no-spawn record fixtures for the
other journal families. Full native lifecycle suites separately cover their
normal launches. This is not yet the actual publication integration.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from case_validation_smoke import FIXTURE as VALIDATION
from terminal_case_activity_smoke import CONTROLLED,connect
from job_release_smoke import wait_file

NATIVE = '''
import sys
from sqlalchemy import select,func,delete
from app.models import NativeCliSession,NativeVizSession,NativeRequest
native_owners={};native_saved={}
MODELS={'terminal':NativeSession,'cli':NativeCliSession,'viz':NativeVizSession,'request':NativeRequest}
@app.get('/api/v1/fixture/native-counts')
async def native_counts(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  return {kind:await db.scalar(select(func.count()).select_from(model).where(model.created_by==user.id)) for kind,model in MODELS.items()}

@app.post('/api/v1/fixture/native-create/{kind}')
async def native_create(kind:str,case_id:int,user=Depends(get_current_user)):
 from app.services import terminal_records,codex_records,viz_records,native_request_records
 from app.services.owned_terminal import OwnedTerminal
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  folder=storage._fs_path(case.s3_prefix,'.').resolve();nonce=uuid.uuid4().hex*2
  if kind=='terminal':
   selected=OwnedTerminal().configure('cmd',cwd=str(folder),cols=80,rows=24,distro='vibeflow-linux')
   selected.update(source_prefix=None,staging_prefix=None)
   owner=await terminal_records.create(case.id,user.id,selected)
  elif kind=='cli':
   selected={'case_id':case.id,'conversation_id':None,'user_id':user.id,'runtime':'windows',
    'program':sys.executable,'program_sha256':'0'*64,'argv':[sys.executable],'cwd':str(folder),
    'auth_home':str(folder/'.private-fixture'),'auth_config_sha256':'0'*64,
    'source_prefix':case.s3_prefix,'staging_prefix':None,'permission_mode':'approve_writes',
    'transport':{key:'0'*64 for key in ('profile_sha256','bridge_sha256','identity_sha256')},'nonce':nonce}
   selected['transport']['profile_sha256']=codex_records.digest({'argv':selected['argv'],'cwd':selected['cwd'],'nonce':nonce})
   owner=await codex_records.create(str(uuid.uuid4()),selected,db=db)
  elif kind=='viz':
   selected={'nonce':nonce,'python':sys.executable,'case_root':str(folder),'source_prefix':case.s3_prefix,
    'staging_prefix':None,'run_dir':'C-run/test','file_path':'source.stl','input_path':str(folder/'source.stl'),
    'input_fingerprint':{'size':0,'mtime_ns':0},'port':49151,'code':{key:'0'*64 for key in viz_records.CODE_NAMES}}
   owner=await viz_records.create(case.id,user.id,selected,db=db)
  elif kind=='request':
   owner=await native_request_records.create(case.id,user.id,'report.prepare',{'pandoc':sys.executable},db=db)
  else:raise ValueError(kind)
  await db.commit();native_owners[str(owner.id)]=(kind,owner)
  return {'id':owner.id,'kind':kind}

@app.post('/api/v1/fixture/native/{identifier}/{action}')
async def native_change(identifier:str,action:str,user=Depends(get_current_user)):
 kind,owner=native_owners[identifier];model=MODELS[kind]
 if action=='complete_empty':
  from app.services import codex_records,native_request_records
  if kind=='cli':await codex_records.complete(owner,None,None,error='FixtureNeverStarted',not_started=True)
  elif kind=='request':await native_request_records.complete(owner,'FixtureNeverStarted')
  else:raise ValueError('No no-start proof for this family')
  return {'completed':True}
 async with SessionLocal() as db:
  row=await db.get(model,owner.id);assert row.created_by==user.id
  journal='children_json' if kind=='request' else 'process_json'
  if action=='delete_fixture':
   # These fixtures only created records; no process was ever allocated.
   value=json.loads(getattr(row,journal))
   assert value=={} or (kind=='cli' and value.get('root') is None and value.get('bridge') is None)
   await db.execute(delete(model).where(model.id==owner.id));native_owners.pop(identifier)
  elif action=='restore':
   for key,value in native_saved.pop(identifier).items():setattr(row,key,value)
  else:
   native_saved[identifier]={key:getattr(row,key) for key in ('phase','lease_expires_at','ended_at','error',journal)}
   if action=='expired':row.lease_expires_at=now()-timedelta(seconds=60)
   elif action=='invalid':setattr(row,journal,'invalid JSON')
   elif action=='unknown_stop':
    row.phase='failed';row.error='FixtureUnknownStop';row.ended_at=now()
   else:raise ValueError(action)
  await db.commit()
 return {'changed':True}
'''


async def verify(database_url=None):
    api=IsolatedServer('validation-native',app='native_fixture:app',database_url=database_url)
    (api.root/'native_fixture.py').write_text(VALIDATION+'\n'+CONTROLLED+'\n'+NATIVE,encoding='utf8')
    checks=[];receipts=[]
    with api:
        api.create_case('Validation native admission')
        peer=IsolatedServer('validation-native-peer',app='peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+VALIDATION+'\n'+CONTROLLED+'\n'+NATIVE,encoding='utf8')
        with peer:
            peer.token=api.token
            job=api.call('POST',f'/fixture/owner?case_id={api.case_id}')['job_id']
            def op(server,action,expect=200):return server.call('POST',f'/fixture/op?job_id={job}&action={action}',expect=expect)
            def blocked(label):
                for server in (api,peer):
                    reply=op(server,'reserve',409)
                    assert reply['detail']['code']=='validation_native_busy',(label,reply)
                checks.append(label)
            def allowed(label):
                op(peer,'reserve');op(peer,'current');op(peer,'release');checks.append(label)
            def native_change(identifier,action):return api.call('POST',f'/fixture/native/{identifier}/{action}')

            # Every record family must reject creation before inserting a row.
            op(peer,'reserve');before=api.call('GET','/fixture/native-counts')
            for kind in ('terminal','cli','viz','request'):
                response=api.call('POST',f'/fixture/native-create/{kind}?case_id={api.case_id}',expect=409)
                assert response['detail']['code']=='validation_busy',response
                assert api.call('GET','/fixture/native-counts')==before
                checks.append(kind+'_reservation_blocks_new_native_record')
            op(peer,'release')

            for kind in ('terminal','cli','viz','request'):
                identifier=api.call('POST',f'/fixture/native-create/{kind}?case_id={api.case_id}')['id']
                try:
                    blocked(kind+'_starting_or_active_blocks_validation')
                    for action in ('expired','invalid'):
                        native_change(identifier,action)
                        try:blocked(kind+'_'+action+'_still_blocks_validation')
                        finally:native_change(identifier,'restore')
                    if kind in {'terminal','viz'}:
                        native_change(identifier,'unknown_stop')
                        try:blocked(kind+'_missing_stop_blocks_validation')
                        finally:native_change(identifier,'restore')
                    else:
                        native_change(identifier,'complete_empty')
                        allowed(kind+'_verified_no_start_or_empty_terminal_journal_allows_validation')
                finally:native_change(identifier,'delete_fixture')

            # Real terminal: selection completed but no native reservation yet.
            before=api.call('GET','/fixture/native-counts')
            (api.root/'before-create.armed').touch()
            connecting=asyncio.create_task(connect(api,rejected=True))
            try:
                await asyncio.to_thread(wait_file,api.root/'before-create.ready',seconds=20)
                op(peer,'reserve')
            finally:
                (api.root/'before-create.release').touch();(api.root/'before-create.armed').unlink(missing_ok=True)
            await asyncio.wait_for(connecting,30)
            assert api.call('GET','/fixture/native-counts')==before
            op(peer,'release');checks.append('selection_to_create_race_rejects_without_native_row_or_spawn')

            sid=None
            (api.root/'starting.armed').touch()
            connecting=asyncio.create_task(connect(api))
            try:
                await asyncio.to_thread(wait_file,api.root/'starting.ready',seconds=20)
                blocked('real_terminal_reserved_before_native_start_blocks_validation')
            finally:
                (api.root/'starting.release').touch();(api.root/'starting.armed').unlink(missing_ok=True)
                ready=await asyncio.wait_for(connecting,30);sid=ready['session']
            try:
                blocked('real_active_terminal_blocks_validation_on_both_APIs')
                state=peer.call('GET',f'/terminal/sessions/{sid}');receipts.append(state)
                assert state['process']['root'] and state['process']['gate'] and state['phase']=='active'
                op(api,'heartbeat')
                (api.root/'closing.armed').touch()
                closing=asyncio.create_task(asyncio.to_thread(api.call,'DELETE',f'/terminal/sessions/{sid}'))
                try:
                    await asyncio.to_thread(wait_file,api.root/'closing.ready',seconds=20)
                    blocked('real_native_exit_before_durable_completion_still_blocks_validation')
                finally:
                    (api.root/'closing.release').touch();(api.root/'closing.armed').unlink(missing_ok=True)
                    await asyncio.wait_for(closing,30)
                final=peer.call('GET',f'/terminal/sessions/{sid}');receipts.append(final)
                assert final['cleanup_verified'] is True and final['phase']=='closed'
                allowed('real_verified_close_allows_validation')
                for action in ('expired','invalid','missing_stop','unregistered',
                        'staging_directory','staging_manifest','staging_cleanup','staging_invalid'):
                    api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':action})
                    try:blocked('real_history_'+action+'_blocks_validation')
                    finally:api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'restore'})
                for action in ('failed_verified','staging_cleared'):
                    api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':action})
                    try:allowed('real_history_'+action+'_allows_validation')
                    finally:api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'restore'})
            finally:
                if sid is not None:api.call('DELETE',f'/terminal/sessions/{sid}')
    result={'passed':True,'checks':checks,'count':len(checks),'receipts':receipts,
        'api_root':str(api.root),'peer_root':str(peer.root),'publication_integration':False,
        'job_writer_exclusion':False,'scope':'case-bound terminal/CLI/viz/request journal lifetimes; real terminal plus no-spawn fixtures'}
    (api.root/'native-validation-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='validation-native-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_validationnative_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_validationnative_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await verify(target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('VALIDATION_NATIVE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
