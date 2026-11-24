"""Real terminal staging with a controlled S3 facade, conflict and case fencing."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

import aiohttp
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic

CONTROLLED='''from pathlib import Path
import asyncio,json,time
from fastapi import Depends,HTTPException
from app.main import app
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.models import NativeSession,Case
from app.services import trash
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select,func
from app.routers import terminal
root=Path(__file__).parent;enabled=False;blocked=set();mode='normal';tasks={}
removed=set();rmtree=terminal.shutil.rmtree
def removing(path,*args,**kwargs):
 if mode=='cleanup_failure':raise PermissionError('isolated terminal cleanup failure')
 if mode=='cleanup_retry' and str(path) not in removed:
  removed.add(str(path));raise PermissionError('isolated transient terminal directory lock')
 return rmtree(path,*args,**kwargs)
terminal.shutil.rmtree=removing
original_use=storage._use_s3;unavailable=trash.case_unavailable
storage._use_s3=lambda:enabled or original_use()
for suffix in ('put','get','size','get_range','delete','exists','list'):
 setattr(storage,'_s3_'+suffix,getattr(storage,'_fs_'+suffix))
trash.case_unavailable=lambda case_id,workspace_id:case_id in blocked or unavailable(case_id,workspace_id)
prepare=storage.prepare_job_staging
def preparing(*args):
 value=prepare(*args)
 if mode in {'cancel_clone','selection_commit_failure'}:
  marker=root/'cloned.json';temporary=marker.with_suffix('.writing');temporary.write_text(json.dumps({'stage':value}));temporary.replace(marker)
 if mode=='cancel_clone':
  while not (root/'release-clone').exists():time.sleep(.02)
 return value
storage.prepare_job_staging=preparing
from staging_test_facade import install
install()
from app.services import staging_sync
sync=staging_sync.collect
def syncing(*args):
 if mode=='sync_hold':
  (root/'sync-started').touch()
  while not (root/'release-sync').exists():time.sleep(.02)
 return sync(*args)
staging_sync.collect=syncing
new=terminal._new_session
async def starting(case_id,*args):
 tasks[case_id]=asyncio.current_task();return await new(case_id,*args)
terminal._new_session=starting
commit=AsyncSession.commit
async def committing(db):
 if mode=='selection_commit_failure':raise OSError('isolated terminal selection commit failure')
 return await commit(db)
AsyncSession.commit=committing

@app.post('/fixture/staging')
async def staging(user=Depends(get_current_user)):
 global enabled
 enabled=True;return {'ok':True}

@app.post('/fixture/mode/{selected}')
async def selected(selected:str,user=Depends(get_current_user)):
 global mode
 mode=selected;return {'ok':True}

@app.post('/fixture/cancel/{case_id}')
async def cancel(case_id:int,user=Depends(get_current_user)):
 tasks[case_id].cancel();(root/'cancel-signalled').touch()
 await asyncio.gather(tasks[case_id],return_exceptions=True)
 return {'ok':True}

@app.get('/fixture/count')
async def count(user=Depends(get_current_user),db=Depends(get_db)):
 return {'count':await db.scalar(select(func.count()).select_from(NativeSession))}

@app.post('/fixture/block/{case_id}')
async def block(case_id:int,user=Depends(get_current_user)):
 blocked.add(case_id);return {'ok':True}

@app.post('/fixture/rebind/{identifier}')
async def rebind(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 case=await db.get(Case,row.case_id);case.s3_prefix=case.s3_prefix.rstrip('/')+'-rebound/'
 await db.commit();return {'ok':True}

@app.get('/fixture/session/{identifier}')
async def session(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 from app.services import terminal_records
 row=await db.get(NativeSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 selected=json.loads(row.profile_json)
 return {'session':terminal_records.public(row),'stage':str(storage._fs_path(selected['staging_prefix'],'.')),
  'cleanup_marker':str(storage._terminal_cleanup_marker(selected['staging_prefix'])),
  'manifest':str(storage._staging_manifest_path(selected['staging_prefix'])),
  'remote':str(storage._fs_path(selected['source_prefix'],'.'))}

@app.post('/fixture/retry/{identifier}')
async def retry(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 selected=json.loads(row.profile_json)
 try:storage.sync_job_staging(selected['staging_prefix'],selected['source_prefix'])
 except storage.StagingConflict:return {'blocked':True}
 raise AssertionError('A terminal cleanup directory was uploaded again')
'''


async def verify(database_url=None,selected=None,boundary=None):
    from staging_liveness_test import instrument,verify_liveness
    server=IsolatedServer('terminal-staging',app='controlled_terminal_staging:app',database_url=database_url)
    (server.root/'controlled_terminal_staging.py').write_text(instrument(CONTROLLED,boundary),encoding='utf8')
    checks=[];runs=[]
    with server as api:
        api.create_case('Terminal staging');workspace=api.call('GET','/workspaces')[0]
        async with aiohttp.ClientSession() as client:
            for mode in ('success','conflict','case_unavailable','source_rebound','sync_hold','cleanup_retry','cleanup_failure'):
                if selected and mode not in selected:continue
                case=api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'staging '+mode});case_id=case['id']
                api.call('PUT',f'/cases/{case_id}/file',{'path':'Ref/terminal-data.txt','content':'BASE'})
                api.call('POST','/fixture/staging',api_prefix=False)
                api.call('POST','/fixture/mode/'+mode,api_prefix=False)
                ws=await client.ws_connect(api.base+'/ws/terminal',params={'token':api.token,'case':case_id,'shell':'cmd'})
                ready=json.loads((await asyncio.wait_for(ws.receive(),25)).data);assert ready['type']=='ready',ready
                identifier=ready['session'];snapshot=api.call('GET','/fixture/session/'+identifier,api_prefix=False)
                stage=Path(snapshot['stage']);remote=Path(snapshot['remote']);manifest=Path(snapshot['manifest']);cleanup_marker=Path(snapshot['cleanup_marker'])
                assert stage.is_relative_to(api.root/'files') and stage.exists() and manifest.exists()
                await ws.send_json({'type':'input','data':'echo STAGED> Ref\\terminal-data.txt\r\n'})
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if (stage/'Ref/terminal-data.txt').read_text().strip()=='STAGED':break
                    await asyncio.sleep(.03)
                else:raise AssertionError('Terminal did not modify its private staging')
                assert (remote/'Ref/terminal-data.txt').read_text()=='BASE'
                if mode=='conflict':api.call('PUT',f'/cases/{case_id}/file',{'path':'Ref/terminal-data.txt','content':'REMOTE'})
                if mode=='case_unavailable':api.call('POST','/fixture/block/'+str(case_id),api_prefix=False)
                if mode=='source_rebound':api.call('POST','/fixture/rebind/'+identifier,api_prefix=False)
                if mode=='sync_hold':
                    async def closing():
                        async with client.delete(api.base+'/api/v1/terminal/sessions/'+identifier,headers={'Authorization':'Bearer '+api.token}) as response:
                            assert response.status==200,await response.text()
                    operation=asyncio.create_task(closing())
                    deadline=time.monotonic()+15
                    while not (api.root/'sync-started').exists():
                        if time.monotonic()>=deadline:raise TimeoutError('Terminal did not reach its synchronization barrier')
                        await asyncio.sleep(.02)
                    current=api.call('GET','/terminal/sessions/'+identifier)
                    listed=next(item for item in api.call('GET','/terminal/sessions')['sessions'] if item['id']==identifier)
                    assert listed['alive'] is False and listed['finalizing'] is True
                    assert current['phase']=='active' and current['ended_at'] is None and current['stop'] is None
                    assert (remote/'Ref/terminal-data.txt').read_text()=='BASE' and not operation.done()
                    from app.services.process_identity_native import probe
                    assert probe(current['process']['root']['host'])['state']=='exited'
                    await verify_liveness(api,case_id,'terminal',boundary)
                    (api.root/'release-sync').touch();await asyncio.wait_for(operation,15)
                else:api.call('DELETE','/terminal/sessions/'+identifier,expect=200 if mode in {'success','cleanup_retry'} else 502)
                await ws.close();result=api.call('GET','/terminal/sessions/'+identifier)
                assert result['cleanup_verified'] is True and result['stop']['verified'] is True
                if mode in {'success','sync_hold','cleanup_retry'}:
                    assert result['phase']=='closed' and (remote/'Ref/terminal-data.txt').read_text().strip()=='STAGED'
                    assert not stage.exists() and not manifest.exists() and not cleanup_marker.exists()
                elif mode=='cleanup_failure':
                    assert result['phase']=='failed' and result['error']=='TerminalStagingCleanupError',result
                    assert stage.exists() and manifest.exists() and cleanup_marker.exists()
                    assert json.loads(cleanup_marker.read_text())['source_synchronized'] is True
                    assert (remote/'Ref/terminal-data.txt').read_text().strip()=='STAGED'
                    assert api.call('POST','/fixture/retry/'+identifier,api_prefix=False)['blocked'] is True
                else:
                    assert result['phase']=='failed' and result['error']==('StagingConflict' if mode=='conflict' else 'CaseWriteConflict'),result
                    assert stage.exists() and manifest.exists() and (stage/'Ref/terminal-data.txt').read_text().strip()=='STAGED'
                    assert (remote/'Ref/terminal-data.txt').read_text()==('REMOTE' if mode=='conflict' else 'BASE')
                checks.append(mode);runs.append({'mode':mode,'session':result,'staging':snapshot})
                print('PASS terminal_staging_'+mode,flush=True)
            for mode in ('cancel_clone','selection_commit_failure'):
                if selected and mode not in selected:continue
                case=api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'staging '+mode});case_id=case['id']
                api.call('PUT',f'/cases/{case_id}/file',{'path':'Ref/terminal-data.txt','content':'BASE'})
                marker=api.root/'cloned.json';marker.unlink(missing_ok=True)
                before=api.call('GET','/fixture/count',api_prefix=False)['count']
                api.call('POST','/fixture/mode/'+mode,api_prefix=False)
                ws=await client.ws_connect(api.base+'/ws/terminal',params={'token':api.token,'case':case_id,'shell':'cmd'})
                deadline=time.monotonic()+10
                while not marker.exists():
                    if time.monotonic()>=deadline:raise TimeoutError('Staging copy did not finish')
                    await asyncio.sleep(.02)
                prefix=json.loads(marker.read_text())['stage'];stage=api.root/'files'/prefix
                assert stage.is_relative_to(api.root/'files')
                manifest=stage.parent/(stage.name+'.manifest.json')
                if mode=='cancel_clone':
                    assert stage.exists()
                    async def cancelling():
                        async with client.post(api.base+'/fixture/cancel/'+str(case_id),headers={'Authorization':'Bearer '+api.token}) as response:
                            assert response.status==200,await response.text()
                    task=asyncio.create_task(cancelling())
                    deadline=time.monotonic()+10
                    while not (api.root/'cancel-signalled').exists():
                        if time.monotonic()>=deadline:raise TimeoutError('Terminal startup cancellation was not delivered')
                        await asyncio.sleep(.02)
                    (api.root/'release-clone').touch();await asyncio.wait_for(task,15)
                else:
                    failure=json.loads((await asyncio.wait_for(ws.receive(),15)).data);assert failure['type']=='error',failure
                await ws.close();api.call('POST','/fixture/mode/normal',api_prefix=False)
                assert api.call('GET','/fixture/count',api_prefix=False)['count']==before
                assert not stage.exists() and not manifest.exists()
                assert api.call('GET',f'/cases/{case_id}/file?path=Ref/terminal-data.txt')['content']=='BASE'
                checks.append(mode);runs.append({'mode':mode,'stage':str(stage),'no_owner_allocated':True})
                print('PASS terminal_staging_'+mode,flush=True)
    value={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),
      'real_terminal':True,'controlled_s3_facade':True,'actual_remote_s3_service_used':False}
    (server.root/'staging-result.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf8')
    return value


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='terminal-staging-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_termstage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_termstage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('TERMINAL_STAGING_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
