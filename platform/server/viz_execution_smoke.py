"""Real VTK rendering, owned process trees and faults on disposable databases."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

import aiohttp
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity

STL='''solid cube
 facet normal 0 0 1
  outer loop
   vertex 0 0 0
   vertex 1 0 0
   vertex 0 1 0
  endloop
 endfacet
endsolid cube
'''

CONTROLLED='''import asyncio,contextvars,copy,json
from datetime import timedelta
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select,update,func
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import NativeVizSession,Job,Case
from app.routers import viz
from app.services import viz_records as records,viz_sessions
from app.services.owned_process import OwnedProcess
from app.services.durable_events import now
root=Path(__file__).parent;mode='normal';settings={};nonces={};foreign=None
viz.VIZ_IDLE_CHECK_INTERVAL=.05
viz_sessions.HEARTBEAT_INTERVAL=.2
failure=contextvars.ContextVar('viz_commit_fault',default=False)
commit=AsyncSession.commit
async def committing(db):
 if failure.get():raise OSError('isolated viz commit failure')
 return await commit(db)
AsyncSession.commit=committing
create=records.create
async def creating(*args,**kwargs):
 owner=await create(*args,**kwargs);settings[owner.id]=mode;nonces[owner.nonce]=mode;return owner
records.create=creating
register=records.register_root
async def registering(owner,birth):
 selected=settings[owner.id]
 if selected in {'hold','disconnect','cancel_startup','source_change','input_change','late_lease'}:
  marker=root/('gated-'+str(owner.id)+'.json');temporary=marker.with_suffix('.writing')
  temporary.write_text(json.dumps(birth));temporary.replace(marker)
  while not (root/('release-'+str(owner.id))).exists():await asyncio.sleep(.02)
 if selected=='bad_root':
  birth=copy.deepcopy(birth);birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
 if selected=='late_lease':
  async with SessionLocal() as db:
   await db.execute(update(NativeVizSession).where(NativeVizSession.id==owner.id).values(lease_expires_at=now()-timedelta(seconds=1)));await db.commit()
 token=failure.set(selected=='root_commit')
 try:return await register(owner,birth)
 finally:failure.reset(token)
records.register_root=registering
ready=records.mark_ready
async def marking(owner,session):
 selected=settings[owner.id]
 if selected=='bad_ready':
  session.ready_birth=copy.deepcopy(session.ready_birth);birth=session.ready_birth['process'];birth['creation_time_100ns']=str(int(birth['creation_time_100ns'])+1)
 if selected=='foreign_ready':session.ready_birth={**session.ready_birth,'process':foreign}
 if selected=='ready_profile':session.ready_birth={**session.ready_birth,'profile_sha256':'0'*64}
 token=failure.set(selected=='ready_commit')
 try:return await ready(owner,session)
 finally:failure.reset(token)
records.mark_ready=marking
release=OwnedProcess.release
async def releasing(self):
 if nonces.get(self.nonce)=='release_failure':raise OSError('isolated release failure')
 return await release(self)
OwnedProcess.release=releasing
stop=OwnedProcess.stop
async def stopping(self,**kwargs):
 value=await stop(self,**kwargs)
 if nonces.get(self.nonce)=='unverified_stop':value['verified']=False;value['error']='OSError'
 return value
OwnedProcess.stop=stopping
heartbeat=records.heartbeat
async def beating(owner,*args):
 if settings.get(owner.id)=='heartbeat_failure':raise OSError('isolated heartbeat failure')
 return await heartbeat(owner,*args)
records.heartbeat=beating
complete=records.complete
async def completing(owner,*args,**kwargs):
 token=failure.set(settings.get(owner.id)=='receipt_failure')
 try:return await complete(owner,*args,**kwargs)
 finally:failure.reset(token)
records.complete=completing

@app.post('/fixture/mode/{selected}')
async def select_mode(selected:str,body:dict={},user=Depends(get_current_user)):
 global mode,foreign
 mode=selected;foreign=body.get('foreign')
 viz_sessions.READY_TIMEOUT=.05 if selected=='timeout' else 30
 viz.VIZ_IDLE_SECONDS=.15 if selected=='idle' else 1800
 return {'ok':True}

@app.get('/fixture/state')
async def state(user=Depends(get_current_user),db=Depends(get_db)):
 rows=(await db.scalars(select(NativeVizSession).where(NativeVizSession.created_by==user.id).order_by(NativeVizSession.id))).all()
 return {'sessions':[records.public(row) for row in rows],'jobs':await db.scalar(select(func.count()).select_from(Job))}

@app.post('/fixture/change-source/{identifier}')
async def change_source(identifier:int,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeVizSession,identifier)
 if row is None or row.created_by!=user.id:raise HTTPException(404)
 case=await db.get(Case,row.case_id);case.s3_prefix+='rebound/';await db.commit();return {'ok':True}

@app.get('/fixture/members/{identifier}')
async def members(identifier:int,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeVizSession,identifier)
 if row is None or row.created_by!=user.id:raise HTTPException(404)
 session=viz._sessions[identifier]
 return {'members':session.owned.scope.active_processes()}
'''


async def request(server,method,path,body=None,*,expect=200,raw=False,api=True,token=None):
    headers={'Authorization':'Bearer '+(token or server.token)}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),trust_env=False) as client:
        async with client.request(method,server.base+('/api/v1' if api else '')+path,json=body,headers=headers) as response:
            data=await response.read()
            assert response.status==expect,(method,path,response.status,data[:1500])
            return data if raw else json.loads(data)


async def until(action,predicate,timeout=20):
    deadline=time.monotonic()+timeout;last=None
    while time.monotonic()<deadline:
        last=await action()
        if predicate(last):return last
        await asyncio.sleep(.05)
    raise AssertionError(('Observation timed out',last))


async def verify(database_url=None):
    server=IsolatedServer('viz-owned',app='controlled_viz:app',database_url=database_url)
    (server.root/'controlled_viz.py').write_text(CONTROLLED,encoding='utf8')
    checked=[];births=[]
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(600)'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    sentinel_birth=identity.observe(sentinel.pid)
    try:
        with server:
            async def case(mode):
                await asyncio.to_thread(server.create_case,'viz-'+mode)
                await request(server,'PUT',server.prefix+'/file',{'path':'D-post/cube.stl','content':STL})
                await request(server,'POST','/fixture/mode/'+mode,{'foreign':sentinel_birth},api=False)
            async def state():return await request(server,'GET','/fixture/state',api=False)
            async def one(sid):return await request(server,'GET',f'/viz:sessions/{sid}')
            async def launch(expect=200):return await request(server,'POST',server.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'},expect=expect,raw=expect!=200)
            async def close(sid,expect=200):return await request(server,'DELETE',f'/viz:sessions/{sid}',expect=expect)
            async def observe_closed(sid):
                value=await until(lambda:one(sid),lambda x:x['phase'] in {'closed','failed'} or x['error'] is not None)
                for key in ('process','ready'):
                    birth=(value.get(key) or {}).get('host' if key=='process' else 'process')
                    if birth:births.append(birth);assert identity.probe(birth)['state']=='exited',value
                return value

            await case('normal');session=await launch();sid=session['id'];owner_token=server.token
            assert session['alive'] is True and session['phase']=='active' and session['ready']['process']!=session['process']['host']
            assert (await state())['jobs']==0
            page=await request(server,'GET',f'/viz-proxy/{sid}/index.html?token={server.token}',raw=True,api=False)
            assert b'trame' in page.lower();checked.append('real_vtk_first_frame_and_proxy')
            started=session['last_activity'];assert (await one(sid))['last_activity']>started
            checked.append('proxy_activity')
            second=await launch();assert second['port']!=session['port']
            await launch(expect=409);checked.append('case_and_subscription_limits')
            await close(second['id']);await observe_closed(second['id'])
            await asyncio.to_thread(server.create_case,'viz-other')
            await request(server,'GET',f'/viz:sessions/{sid}',expect=404)
            await request(server,'DELETE',f'/viz:sessions/{sid}',expect=404)
            await request(server,'GET',f'/viz-proxy/{sid}/index.html?token={server.token}',expect=404,raw=True,api=False)
            checked.append('cross_user_rejected');server.token=owner_token;server.case_id=session['case_id'];server.prefix=f"/cases/{session['case_id']}"
            job=await request(server,'POST',server.prefix+'/jobs',{'type':'shell','runtime':'windows','command':'ping -n 120 127.0.0.1 > nul','timeout_seconds':180})
            await until(lambda:request(server,'GET',f"/jobs/{job['id']}"),lambda x:x['status']=='running')
            await asyncio.gather(close(sid),close(sid));closed=await observe_closed(sid)
            assert closed['phase']=='closed' and closed['cleanup_verified'] is True
            assert (await request(server,'GET',f"/jobs/{job['id']}"))['status']=='running'
            checked.append('close_view_does_not_cancel_engineering_job')
            await request(server,'POST',f"/jobs/{job['id']}:cancel")
            await close(sid);assert not await request(server,'GET',server.prefix+'/viz:sessions')
            checked.append('concurrent_and_repeated_verified_close')

            for mode in ('bad_root','root_commit','release_failure','bad_ready','foreign_ready','ready_profile','ready_commit','timeout'):
                await case(mode);await launch(expect=500)
                rows=(await state())['sessions'];assert len(rows)==1,rows
                value=await observe_closed(rows[0]['id'])
                assert value['phase']=='failed' and value['cleanup_verified'] is True,value
                assert identity.probe(sentinel_birth)['state']=='match'
                checked.append(mode)
                print('VIZ_FAULT_OK '+mode,flush=True)

            for mode in ('hold','source_change','input_change','late_lease','disconnect','cancel_startup'):
                await case(mode);task=asyncio.create_task(launch(expect=200 if mode=='hold' else 499 if mode=='disconnect' else 500 if mode=='cancel_startup' else 409))
                rows=await until(state,lambda x:bool(x['sessions']));sid=rows['sessions'][0]['id']
                await until(lambda:asyncio.to_thread((server.root/f'gated-{sid}.json').exists),bool)
                assert (await request(server,'GET',f'/fixture/members/{sid}',api=False))['members']==1
                before=time.monotonic()
                await request(server,'PUT',server.prefix+'/file',{'path':'D-post/during-start.txt','content':'write allowed'})
                await request(server,'GET','/api/health',api=False)
                assert time.monotonic()-before<2
                if mode=='hold':
                    await request(server,'POST',server.prefix+':copy',{'name':'blocked while rendering'},expect=409)
                    await request(server,'DELETE',server.prefix,expect=409)
                    checked.append('durable_starting_blocks_copy_and_trash')
                if mode=='source_change':await request(server,'POST',f'/fixture/change-source/{sid}',api=False)
                if mode=='input_change':await request(server,'PUT',server.prefix+'/file',{'path':'D-post/cube.stl','content':STL+'\n'})
                if mode=='disconnect':task.cancel();await asyncio.gather(task,return_exceptions=True)
                elif mode=='cancel_startup':
                    await request(server,'POST','/runtime/processes:cancel',{'case_id':server.case_id})
                else:(server.root/f'release-{sid}').touch()
                if mode!='disconnect':
                    response=await task
                    if mode=='late_lease':
                        assert json.loads(response)['detail']['code']=='recovery_required',response
                if mode=='hold':await close(sid)
                await observe_closed(sid);checked.append(mode);print('VIZ_START_OK '+mode,flush=True)

            for mode in ('idle','heartbeat_failure','unverified_stop','receipt_failure'):
                await case(mode)
                # Renew can intentionally fail during the expensive real first
                # frame. For this fault, turn it on after successful readiness.
                if mode=='heartbeat_failure':
                    await request(server,'POST','/fixture/mode/normal',api=False)
                    session=await launch()
                    await request(server,'POST','/fixture/fail-heartbeat/'+str(session['id']),api=False)
                else:session=await launch()
                sid=session['id']
                if mode in {'unverified_stop','receipt_failure'}:await close(sid,expect=502)
                if mode=='receipt_failure':
                    value=await one(sid);assert value['phase']=='active' and value['ended_at'] is None and value['stop'] is None,value
                    assert identity.probe(session['ready']['process'])['state']=='exited'
                    assert identity.probe(session['process']['host'])['state']=='exited'
                else:
                    value=await observe_closed(sid)
                    assert value['cleanup_verified'] is (False if mode=='unverified_stop' else True),value
                checked.append(mode)
                print('VIZ_CLOSE_OK '+mode,flush=True)
            assert identity.probe(sentinel_birth)['state']=='match'
            await case('normal');session=await launch();sid=session['id']
            await request(server,'PUT',server.prefix+'/file',{'path':'D-post/scenes.py','content':'import os\nos._exit(7)\n'})
            value=await observe_closed(sid)
            assert value['phase']=='failed' and value['exit_code']==7 and value['cleanup_verified'] is True,value
            checked.append('actual_kernel_nonzero_exit_preserved')
            for how in ('explicit','case_cancel','shutdown_hook','owner_death'):
                await case('normal');session=await launch();sid=session['id']
                marker=server.root/f'leaf-{how}.json'
                child_code='import json,os,time;from pathlib import Path;Path('+repr(str(marker))+').write_text(json.dumps({"pid":os.getpid()}));time.sleep(300)'
                argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S','-c',child_code]
                script='import subprocess\nsubprocess.Popen('+repr(argv)+', creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)\n'
                await request(server,'PUT',server.prefix+'/file',{'path':'D-post/scenes.py','content':script})
                await until(lambda:asyncio.to_thread(marker.exists),bool,timeout=15)
                leaf=identity.observe(json.loads(marker.read_text())['pid'])
                if how=='explicit':await close(sid)
                elif how=='case_cancel':
                    response=await request(server,'POST','/runtime/processes:cancel',{'case_id':server.case_id})
                    assert response['ok'] and response['cancelled']['viz']==1,response
                elif how=='shutdown_hook':
                    await request(server,'POST','/fixture/close-all-viz',api=False,expect=500,raw=True)
                    checked.append('shutdown_hook_retains_prior_cleanup_failures')
                else:
                    # Use the actual test-owned API creation handle. Do not
                    # recover or signal historical kernel PIDs after restart.
                    server.proc.kill();server.proc.wait(timeout=10)
                await until(lambda:asyncio.to_thread(identity.probe,leaf),lambda x:x['state']=='exited')
                for birth in (session['ready']['process'],session['process']['host']):
                    await until(lambda:asyncio.to_thread(identity.probe,birth),lambda x:x['state']=='exited')
                births.extend((leaf,session['ready']['process'],session['process']['host']))
                if how!='owner_death':await observe_closed(sid)
                else:
                    server.proc=subprocess.Popen([sys.executable,'-m','uvicorn',server.app,'--app-dir',str(SERVER),'--host','127.0.0.1',
                        '--port',str(server.port),'--no-access-log'],cwd=server.root,env=server.env,stdout=server.log,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    async def health():
                        try:return await request(server,'GET','/api/health',api=False)
                        except aiohttp.ClientError:return {}
                    await until(health,lambda x:x.get('ok'))
                    before=await one(sid)
                    assert before['owner_available'] is False and before['alive'] is None and before['stop'] is None and before['ended_at'] is None,before
                    await request(server,'POST',f'/fixture/expire/{sid}',api=False)
                    unknown=await one(sid);assert unknown['phase']=='recovery_required' and unknown['stop'] is None,unknown
                    await request(server,'POST',server.prefix+':copy',{'name':'blocked unknown owner'},expect=409)
                    await request(server,'DELETE',server.prefix,expect=409)
                    await close(sid,expect=409)
                    again=await one(sid);assert again==unknown
                    history=await request(server,'GET',f"/viz:sessions/{closed['id']}",token=owner_token)
                    assert history['phase']=='closed' and history['cleanup_verified'] is True and history['owner_available'] is False,history
                    checked.append('restart_readonly_history_without_replay')
                    checked.append('unknown_owner_blocks_copy_and_trash')
                checked.append('detached_child_'+how);print('VIZ_TREE_OK '+how,flush=True)
            assert identity.probe(sentinel_birth)['state']=='match'
    finally:
        sentinel.terminate();sentinel.wait(timeout=5)
    result={'passed':True,'checks':checked,'count':len(checked),'births_verified_exited':len(births),'foreign_sentinel_untouched':True,'evidence':str(server.root)}
    (server.root/'viz-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


CONTROLLED+='''
@app.post('/fixture/fail-heartbeat/{identifier}')
async def fail_heartbeat(identifier:int,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeVizSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 settings[identifier]='heartbeat_failure';return {'ok':True}

@app.post('/fixture/expire/{identifier}')
async def expire(identifier:int,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeVizSession,identifier)
 if row is None or row.created_by!=user.id:raise HTTPException(404)
 row.lease_expires_at=now()-timedelta(seconds=1);await db.commit();return {'ok':True}

@app.post('/fixture/close-all-viz')
async def close_all(user=Depends(get_current_user)):
 await viz.close_all_viz_sessions();return {'ok':True}
'''


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='viz-owned-evidence-',dir=SERVER.parents[1]/'tmp'));results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_viz_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_viz_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10);created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            await asyncio.to_thread(alembic,url.set(database=name).render_as_string(hide_password=False),report,'upgrade','head')
            results['postgresql']=await verify(url.set(database=name).render_as_string(hide_password=False))
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres';await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('VIZ_OWNED_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
