"""Real approved Agent command staging with a controlled file-backed S3 facade."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import uuid

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from agent_shell_smoke import CONTROLLED as SHELL_FIXTURE,frames
from viz_execution_smoke import request,until

CONTROLLED=SHELL_FIXTURE+'''
import time
from app import storage
from app.services import agent_shell,trash
from app.routers import terminal
enabled=False;blocked=set();removed=set()
original_use=storage._use_s3;unavailable=trash.case_unavailable
storage._use_s3=lambda:enabled or original_use()
for suffix in ('put','get','size','get_range','delete','exists','list'):
 setattr(storage,'_s3_'+suffix,getattr(storage,'_fs_'+suffix))
trash.case_unavailable=lambda case_id,workspace_id:case_id in blocked or unavailable(case_id,workspace_id)
rmtree=terminal.shutil.rmtree
def removing(path,*args,**kwargs):
 if mode=='cleanup_failure':raise PermissionError('isolated Agent staging directory lock')
 if mode=='cleanup_retry' and str(path) not in removed:
  removed.add(str(path));raise PermissionError('isolated transient Agent staging directory lock')
 return rmtree(path,*args,**kwargs)
terminal.shutil.rmtree=removing
prepare=storage.prepare_job_staging
def preparing(*args):
 value=prepare(*args)
 if mode in {'cancel_clone','selection_commit_failure'}:
  marker=root/'cloned.json';temp=marker.with_suffix('.writing');temp.write_text(json.dumps({'stage':value}));temp.replace(marker)
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
owned_create=records.create
async def creating_owned(*args,**kwargs):
 owner=await owned_create(*args,**kwargs)
 if mode=='selection_commit_failure':kwargs['db'].info['agent_selection_commit']=True
 return owner
records.create=creating_owned
owned_commit=AsyncSession.commit
async def committing_owned(db):
 if db.info.pop('agent_selection_commit',False):raise OSError('isolated Agent selection commit failure')
 return await owned_commit(db)
AsyncSession.commit=committing_owned
owned_finish=records.finish
async def finished(owner,*args):
 result=await owned_finish(owner,*args)
 if settings.get(owner.id) in {'conflict','case_unavailable','source_rebound'}:
  async with SessionLocal() as db:
   row=await db.get(NativeRequest,owner.id);case=await db.get(Case,row.case_id)
   if settings[owner.id]=='case_unavailable':blocked.add(case.id)
   if settings[owner.id]=='source_rebound':case.s3_prefix+='rebound/';await db.commit()
   if settings[owner.id]=='conflict':storage._fs_put(case.s3_prefix,'Ref/agent-data.txt',b'REMOTE')
 return result
records.finish=finished
execute=agent_shell.execute;tasks={}
async def executing(case_id,*args,**kwargs):
 tasks[case_id]=asyncio.current_task();return await execute(case_id,*args,**kwargs)
agent_shell.execute=executing

@app.post('/fixture/staging')
async def staging(user=Depends(get_current_user)):
 global enabled
 enabled=True;return {'ok':True}

@app.post('/fixture/cancel/{case_id}')
async def cancel(case_id:int,user=Depends(get_current_user)):
 tasks[case_id].cancel();(root/'cancel-signalled').touch()
 await asyncio.gather(tasks[case_id],return_exceptions=True);return {'ok':True}

@app.get('/fixture/staging/{tid}')
async def stage(tid:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.scalar(select(NativeRequest).where(NativeRequest.tool_call_id==tid,NativeRequest.created_by==user.id))
 if row is None:raise HTTPException(404)
 selected=json.loads(row.profile_json)
 return {'execution':records.public(row),'stage':str(storage._fs_path(selected['staging_prefix'],'.')),
  'manifest':str(storage._staging_manifest_path(selected['staging_prefix'])),
  'cleanup_marker':str(storage._terminal_cleanup_marker(selected['staging_prefix'])),
  'remote':str(storage._fs_path(selected['source_prefix'],'.'))}

@app.post('/fixture/resync/{tid}')
async def resync(tid:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.scalar(select(NativeRequest).where(NativeRequest.tool_call_id==tid,NativeRequest.created_by==user.id))
 selected=json.loads(row.profile_json)
 try:storage.sync_job_staging(selected['staging_prefix'],selected['source_prefix'])
 except storage.StagingConflict:return {'blocked':True}
 raise AssertionError('A partially cleaned Agent staging tree was uploaded again')
'''


async def verify(database_url=None,selected=None,boundary=None):
    from staging_liveness_test import instrument,verify_liveness
    server=IsolatedServer('agent-shell-staging',app='controlled_agent_staging:app',database_url=database_url)
    (server.root/'controlled_agent_staging.py').write_text(instrument(CONTROLLED,boundary),encoding='utf8');checks=[];runs=[]
    with server:
        server.create_case('Agent staging');workspace=server.call('GET','/workspaces')[0]
        await request(server,'POST','/fixture/staging',api=False)
        for mode in ('success','conflict','case_unavailable','source_rebound','sync_hold','cleanup_retry','cleanup_failure','cancel_clone','selection_commit_failure'):
            if selected and mode not in selected:continue
            await request(server,'POST','/fixture/mode/normal',api=False)
            case=await request(server,'POST',f"/workspaces/{workspace['id']}/cases",{'name':'Agent '+mode});case_id=case['id']
            await request(server,'PUT',f'/cases/{case_id}/file',{'path':'Ref/agent-data.txt','content':'BASE'})
            tc=await request(server,'POST','/fixture/tool',{'case_id':case_id,'args':{'command':'echo STAGED> Ref\\agent-data.txt','runtime':'windows'}},api=False)
            cloned=server.root/'cloned.json';cloned.unlink(missing_ok=True)
            before=len((await request(server,'GET','/fixture/state',api=False))['executions'])
            await request(server,'POST','/fixture/mode/'+mode,api=False)
            operation=asyncio.create_task(request(server,'POST',f"/conversations/{tc['cid']}/tool-calls/{tc['tid']}:approve",{},raw=True,expect=500 if mode=='cancel_clone' else 200))
            if mode in {'cancel_clone','selection_commit_failure'}:
                await until(lambda:asyncio.to_thread(cloned.exists),bool)
                prefix=json.loads(cloned.read_text())['stage'];stage=server.root/'files'/prefix
                assert stage.is_relative_to(server.root/'files')
                if mode=='cancel_clone':
                    cancelling=asyncio.create_task(request(server,'POST',f'/fixture/cancel/{case_id}',api=False))
                    await until(lambda:asyncio.to_thread((server.root/'cancel-signalled').exists),bool)
                    (server.root/'release-clone').touch();await cancelling
                await operation;await request(server,'POST','/fixture/mode/normal',api=False)
                assert len((await request(server,'GET','/fixture/state',api=False))['executions'])==before
                assert not stage.exists() and not (stage.parent/(stage.name+'.manifest.json')).exists()
                checks.append(mode);runs.append({'mode':mode,'no_owner_allocated':True});print('AGENT_STAGING_OK '+mode,flush=True);continue
            if mode=='sync_hold':
                await until(lambda:asyncio.to_thread((server.root/'sync-started').exists),bool)
                pending=await request(server,'GET',f"/fixture/staging/{tc['tid']}",api=False)
                assert pending['execution']['phase']=='active' and pending['execution']['cleanup_verified'] is True and not operation.done(),pending
                assert (Path(pending['remote'])/'Ref/agent-data.txt').read_text()=='BASE'
                await verify_liveness(server,case_id,'agent',boundary)
                (server.root/'release-sync').touch()
            response=next(event for event in frames(await operation) if event['type']=='tool.result')
            snapshot=await request(server,'GET',f"/fixture/staging/{tc['tid']}",api=False)
            row=snapshot['execution'];stage=Path(snapshot['stage']);remote=Path(snapshot['remote']);manifest=Path(snapshot['manifest']);marker=Path(snapshot['cleanup_marker'])
            assert row['cleanup_verified'] is True,row
            if mode in {'success','sync_hold','cleanup_retry'}:
                assert response['ok'] is True and row['phase']=='succeeded',response
                assert (remote/'Ref/agent-data.txt').read_text().strip()=='STAGED'
                assert not stage.exists() and not manifest.exists() and not marker.exists()
            else:
                assert not response['ok'] and row['phase']=='failed',response
                assert stage.exists() and manifest.exists()
                if mode=='cleanup_failure':
                    assert marker.exists() and json.loads(marker.read_text())['source_synchronized'] is True
                    assert (remote/'Ref/agent-data.txt').read_text().strip()=='STAGED'
                    assert (await request(server,'POST',f"/fixture/resync/{tc['tid']}",api=False))['blocked'] is True
                else:assert (remote/'Ref/agent-data.txt').read_text()==('REMOTE' if mode=='conflict' else 'BASE')
            checks.append(mode);runs.append({'mode':mode,'snapshot':snapshot,'tool_result':response});print('AGENT_STAGING_OK '+mode,flush=True)
            await request(server,'DELETE',f"/conversations/{tc['cid']}",expect=200 if mode in {'success','sync_hold','cleanup_retry'} else 409)
    value={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'real_approved_command':True,'controlled_s3_facade':True,'actual_remote_s3_service_used':False,'evidence':str(server.root)}
    (server.root/'staging-result.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf8');return value


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='agent-staging-evidence-',dir=SERVER.parents[1]/'tmp'));results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url);assert url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_agentstage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_agentstage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10);created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True;temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head');results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres';await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('AGENT_STAGING_ALL_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
