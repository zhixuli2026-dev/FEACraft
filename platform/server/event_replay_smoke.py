"""Real HTTP/WS durable replay, competing APIs and original completion visibility."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

import aiohttp

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

FIXTURE='''
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.services.durable_events import append_event,json_text,now
@app.post('/api/v1/fixture/events/{case_id}')
async def seed(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db)
 ids=[]
 for i in range(body.get('count',1)):
  e=await append_event(db,case_id,'fixture.changed',{'index':i});ids.append({'id':e.id,'seq':e.seq})
 if body.get('rollback'):await db.rollback()
 else:await db.commit()
 return {'events':ids}
@app.post('/api/v1/fixture/pending/{case_id}')
async def pending(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db)
 j=Job(case_id=case_id,type='shell',command='event visibility fixture',status='succeeded',exit_code=0,ended_at=now())
 db.add(j);await db.flush()
 a=JobAttempt(job_id=j.id,number=1,phase='succeeded',output_manifest_json=json_text({'completion':{
  'schema':'vibeflow.job-completion.v1','state':'pending','alias_error':None,'settled_at':None}}))
 db.add(a);await db.flush()
 e=await append_event(db,case_id,'jobs.updated',{'job_id':j.id,'status':'succeeded'},attempt_id=a.id)
 await append_event(db,case_id,'fixture.after_success',{})
 await db.commit();return {'job_id':j.id,'event_id':e.id,'seq':e.seq}
@app.post('/api/v1/fixture/settle/{job_id}')
async def settle(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db)
 a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id))
 a.output_manifest_json=json_text({'completion':{'schema':'vibeflow.job-completion.v1',
  'state':'settled','alias_error':None,'settled_at':now().isoformat()}})
 await db.commit();return {'ok':True}
@app.post('/api/v1/fixture/transient/{case_id}')
async def transient(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db)
 from app.services.eventbus import publish
 publish(user.id,{'type':'fixture.transient','case_id':case_id})
 return {'ok':True}
'''


def peer(api):
    server=IsolatedServer('events-peer',app='event_peer:app',database_url=api.database_url)
    overrides={k:api.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
    (server.root/'event_peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE,encoding='utf8')
    server.token=api.token
    return server


async def receive_event(ws):
    async with asyncio.timeout(8):
        while True:
            message=await ws.receive()
            assert message.type==aiohttp.WSMsgType.TEXT,message
            event=json.loads(message.data)
            if event.get('event_id') or event['type']=='fixture.transient':return event


async def sockets(api,other_id,reader,last):
    async with aiohttp.ClientSession() as session:
        url=api.base.replace('http','ws',1)+f'/ws/events?case_id={api.case_id}&after={last}&token={api.token}'
        async with session.ws_connect(url) as ws:
            assert (await ws.receive_json())['type']=='ready'
            # Commit on the other API: its local bus cannot reach this socket.
            created=await asyncio.to_thread(reader.call,'POST',f'/fixture/events/{api.case_id}',{'count':3})
            got=[await receive_event(ws) for _ in range(3)]
            assert [(e['event_id'],e['seq']) for e in got]==[(e['id'],e['seq']) for e in created['events']],got
            await asyncio.to_thread(api.call,'POST',f'/fixture/transient/{other_id}')
            await asyncio.to_thread(api.call,'POST',f'/fixture/transient/{api.case_id}')
            hint=await receive_event(ws)
            assert hint['type']=='fixture.transient' and hint['case_id']==api.case_id,hint
        # Disconnect, commit, reconnect with last received cursor.
        created=await asyncio.to_thread(reader.call,'POST',f'/fixture/events/{api.case_id}',{'count':2})
        reconnect=url.replace('after='+str(last),'after='+str(got[-1]['seq']))
        async with session.ws_connect(reconnect) as ws:
            assert (await ws.receive_json())['type']=='ready'
            got=[await receive_event(ws) for _ in range(2)]
            assert [e['event_id'] for e in got]==[e['id'] for e in created['events']]
        async with session.ws_connect(url.replace('after='+str(last),'after=9000000')) as ws:
            assert (await ws.receive_json())['type']=='ready'
            assert (await ws.receive_json())['type']=='cursor.reset'


def verify(database_url=None):
    api=IsolatedServer('event-replay',app='event_fixture:app',database_url=database_url)
    (api.root/'event_fixture.py').write_text(FIXTURE,encoding='utf8')
    checks=[]
    with api:
        api.create_case('event cursor')
        path=api.prefix+'/workflow-events'
        start=api.call('GET',path)['watermark']
        created=api.call('POST',f'/fixture/events/{api.case_id}',{'count':205})['events']
        cursor=start;observed=[]
        while True:
            batch=api.call('GET',path+f'?after={cursor}&limit=73')
            observed+=batch['events'];cursor=batch['next_after']
            if not batch['has_more']:break
        assert [(e['event_id'],e['seq']) for e in observed]==[(e['id'],e['seq']) for e in created]
        assert api.call('GET',path+f'?after={start}&limit=73')['events']==observed[:73]
        checks.append('ordered_pages_stable_IDs_and_repeatable_cursor')
        api.call('POST',f'/fixture/events/{api.case_id}',{'rollback':True})
        assert api.call('GET',path+f'?after={cursor}')['events']==[]
        checks.append('rolled_back_event_not_replayed')
        for query in ('after=-1','after=9007199254740992','limit=201','after=abc'):
            api.call('GET',path+'?'+query,expect=422)
        assert api.call('GET',path+'?after=9000000',expect=409)['detail']['code']=='event_cursor_ahead'
        checks.append('invalid_and_future_cursor_rejected')
        pending=api.call('POST',f'/fixture/pending/{api.case_id}')
        batch=api.call('GET',path+f'?after={cursor}')
        assert batch['blocked'] and batch['next_after']==cursor and not batch['events'],batch
        assert api.call('GET',f'/jobs/{pending["job_id"]}')['status']=='running'
        api.call('POST',f'/fixture/settle/{pending["job_id"]}')
        batch=api.call('GET',path+f'?after={cursor}')
        assert not batch['blocked'] and batch['events'][0]['event_id']==pending['event_id'] and len(batch['events'])==2
        checks.append('pending_success_blocks_cursor_until_actual_visibility_settles')
        workspace=api.call('GET','/workspaces')[0]
        other=api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':'same owner other case'})
        with peer(api) as reader:
            asyncio.run(sockets(api,other['id'],reader,batch['next_after']))
            checks.extend(['other_API_commit_reaches_socket','cross_case_transient_not_forwarded','disconnect_replays_exact_missing_IDs','socket_future_cursor_explicit_reset'])
            stable=reader.call('GET',path+'?limit=200')
            api.proc.terminate();api.proc.wait(timeout=10)
            assert reader.call('GET',path+'?limit=200')==stable
            with peer(api) as restarted:assert restarted.call('GET',path+'?limit=200')==stable
            checks.append('API_death_and_restart_preserve_IDs_and_cursor')
            token=reader.token
            reader.create_case('other owner')
            reader.call('GET',path,expect=(403,404))
            reader.token=None;reader.call('GET',path,expect=401);reader.token=token
            checks.append('cross_owner_and_anonymous_replay_rejected')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='event-replay-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_events_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('EVENT_REPLAY_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
