"""Actual API death after committed delivery intent; isolated SQL and files."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from job_owner_startup_smoke import shared
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity
from app.services.owned_process import wsl_path

FIXTURE = '''import os
os.environ.update(OVERRIDES)
import asyncio,json,time,uuid
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Job,JobAttempt,DurableEvent,User,Plan,Subscription
from app.routers.auth import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_delivery,job_execution,jobrunner,batch_jobs,distributed_queue
from app.services.durable_events import lock_case_execution,json_text
ROOT=Path(__file__).parent
if os.getenv('DELIVERY_FIXTURE_QUEUE'):distributed_queue.QUEUE_NAME=os.environ['DELIVERY_FIXTURE_QUEUE']
original_loop=job_delivery.recover_forever
async def loop(stop):
 if os.getenv('DELIVERY_FIXTURE_PAUSE_SCANNER')=='1':await stop.wait()
 else:await original_loop(stop)
job_delivery.recover_forever=loop
async def hold(value):
 if (ROOT/'pause-launch').exists():
  (ROOT/'held.json').write_text(json.dumps(value))
  deadline=time.monotonic()+45
  while not (ROOT/'continue').exists() and time.monotonic()<deadline:await asyncio.sleep(.02)
  if not (ROOT/'continue').exists():raise RuntimeError('Delivery fixture timed out')
original_start=jobrunner.start_job
async def start(job_id,*args,**kwargs):
 await hold({'jobs':[job_id]})
 return await original_start(job_id,*args,**kwargs)
jobrunner.start_job=start
original_batch=batch_jobs.start_batch
async def batch(state):
 await hold({'jobs':state.job_ids,'batch_id':state.batch_id})
 return await original_batch(state)
batch_jobs.start_batch=batch
@app.get('/api/v1/fixture/rows/{case_id}')
async def rows(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db)
  result=[]
  for j in await db.scalars(select(Job).where(Job.case_id==case_id).order_by(Job.id)):
   a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id).order_by(JobAttempt.number.desc()).limit(1))
   events=[{'id':e.id,'type':e.type,'value':json.loads(e.payload_json)} for e in await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id).order_by(DurableEvent.seq))]
   result.append({'id':j.id,'status':j.status,'phase':a.phase,'attempt_id':a.id,'claim':a.claim_token is not None,
    'identity':a.process_identity_json!='{}','events':events})
  return result
@app.get('/api/v1/fixture/birth')
async def birth(user:User=Depends(get_current_user)):
 from app.services.process_identity_native import observe
 return observe(os.getpid())
@app.post('/api/v1/fixture/recover')
async def recover(user:User=Depends(get_current_user)):
 return await job_delivery.recover_once()
@app.post('/api/v1/fixture/deliver/{job_id}')
async def deliver_one(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db)
 try:return {'result':await job_delivery.deliver(job_id)}
 except job_execution.ExecutionConflict as error:return {'result':error.code}
@app.post('/api/v1/fixture/seed/{case_id}')
async def seed(case_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  spec={'command':data['command'],'runtime':'windows','argv':None,'case_prefix':case.s3_prefix,
   'timeout_seconds':86400,'max_log_bytes':16*1024*1024,'evidence_run_dir':None}
  j,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
   digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec,isolate_default=True)
  if data.get('intent',True):await job_delivery.request(db,j,user_id=user.id)
  identifier=j.id
  if data.get('rollback'):await db.rollback()
  else:await db.commit()
  return {'id':identifier}
@app.post('/api/v1/fixture/damage/{job_id}')
async def damage(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db);await lock_case_execution(db,j.case_id)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id))
  if data['kind']=='spec':
   value=json.loads(a.input_manifest_json);value['execution_spec']['argv']=['unexpected'];a.input_manifest_json=json_text(value)
  else:
   e=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==a.id,DurableEvent.type==job_delivery.EVENT))
   value=json.loads(e.payload_json);value['host_id']='f'*64;e.payload_json=json_text(value)
  await db.commit()
 return {'ok':True}
@app.post('/api/v1/fixture/limit')
async def limit(data:dict,user:User=Depends(get_current_user)):
 from app.services.quota import ensure_user_org
 async with SessionLocal() as db:
  org=await ensure_user_org(db,user)
  p=Plan(code=uuid.uuid4().hex,name='Isolated delivery limit',quotas=json.dumps({'job_concurrent':data['count']}));db.add(p);await db.flush()
  sub=await db.scalar(select(Subscription).where(Subscription.org_id==org.id,Subscription.status=='active'))
  sub.plan_id=p.id;await db.commit()
 return {'ok':True}
'''


def new_api(label,url=None,overrides=None,*,paused=False):
    api=IsolatedServer(label,app='delivery_fixture:app',database_url=url)
    values={**(overrides or {}),'DELIVERY_FIXTURE_PAUSE_SCANNER':'1' if paused else '0'}
    (api.root/'delivery_fixture.py').write_text(FIXTURE.replace('OVERRIDES',repr(values)),encoding='utf8')
    return api


def payload(root,label,runtime='windows'):
    marker=root/(label+'.started');finish=root/(label+'.finish');script=root/(label+'.py')
    native=lambda p:wsl_path(p) if runtime=='wsl' else str(p)
    script.write_text('from pathlib import Path\nimport time\np=Path('+repr(native(marker))+')\n'
        'with p.open("a") as f:f.write("started\\n")\n'
        'deadline=time.monotonic()+60\n'
        'while not Path('+repr(native(finish))+').exists() and time.monotonic()<deadline:time.sleep(.02)\n',encoding='utf8')
    command='python3 '+wsl_path(script) if runtime=='wsl' else subprocess.list2cmdline([sys.executable,str(script)])
    return command,marker,finish


def wait_rows(api,case_id,predicate,seconds=35):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        rows=api.call('GET',f'/fixture/rows/{case_id}')
        if predicate(rows):return rows
        time.sleep(.05)
    raise AssertionError(rows)


def kill_owner(api,birth):
    api.proc.kill();api.proc.wait(timeout=10)
    deadline=time.monotonic()+10
    while identity.probe(birth)['state']!='exited' and time.monotonic()<deadline:time.sleep(.05)
    assert identity.probe(birth)['state']=='exited'


def verify(url=None,*,wsl=False):
    checks=[];receipts=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    for runtime in ('windows','wsl') if wsl else ('windows',):
        owner=new_api('delivery-origin-'+runtime,url,paused=True)
        with owner:
            owner.create_case('Committed delivery survives actual API death')
            command,marker,finish=payload(owner.root,'single',runtime)
            body={'command':command,'runtime':runtime};key=uuid.uuid4().hex
            (owner.root/'pause-launch').touch()
            birth=owner.call('GET','/fixture/birth')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(owner.call,'POST',owner.prefix+'/jobs',body,extra_headers={'Idempotency-Key':key})
                try:
                    wait_file(owner.root/'held.json');before=owner.call('GET',f'/fixture/rows/{owner.case_id}')
                    assert len(before)==1 and before[0]['phase']=='queued' and not before[0]['claim'] and not marker.exists()
                    assert len([e for e in before[0]['events'] if e['type']=='attempt.delivery_requested'])==1
                    kill_owner(owner,birth)
                    try:future.result(timeout=10)
                    except (OSError,ValueError):pass
                    peer=new_api('delivery-restart-'+runtime,url,shared(owner))
                    with peer:
                        peer.token=owner.token;job=before[0]['id']
                        wait_file(marker,seconds=35)
                        peer2=new_api('delivery-second-peer-'+runtime,url,shared(owner))
                        with peer2:
                            peer2.token=owner.token
                            with ThreadPoolExecutor(max_workers=2) as duplicates:
                                [f.result(timeout=30) for f in [duplicates.submit(p.call,'POST','/fixture/recover') for p in (peer,peer2)]]
                            replay=peer2.call('POST',owner.prefix+'/jobs',body,extra_headers={'Idempotency-Key':key})
                            assert replay['id']==job
                            assert marker.read_text().splitlines()==['started']
                            finish.touch()
                            done=peer.wait_job(job,timeout=45)
                            assert done['status']=='succeeded' and done['attempt']['process_stop']['verified']
                            rows=peer.call('GET',f'/fixture/rows/{owner.case_id}')
                            assert len(rows)==1 and rows[0]['attempt_id']==before[0]['attempt_id']
                            assert len([e for e in rows[0]['events'] if e['type']=='attempt.state_changed' and e['value']['status']=='claimed'])==1
                            receipts.append({'runtime':runtime,'before':before,'after':rows,'result':done,'actual_owner_exit':True})
                            passed(runtime+'_real_commit_before_launch_death_recovers_once_with_same_attempt')
                            peer2.create_case('Foreign delivery reader');peer2.call('GET',f'/jobs/{job}',expect=404)
                            passed(runtime+'_recovered_job_retains_access_boundary')
                finally:finish.touch();(owner.root/'continue').touch()
    for maximum,quota in ((1,4),(3,1)):
        owner=new_api('delivery-batch-origin',url,paused=True)
        with owner:
            owner.create_case('Batch delivery capacity after API death')
            owner.call('POST','/fixture/limit',{'count':quota})
            prepared=[payload(owner.root,'batch'+str(i)) for i in range(3)]
            (owner.root/'pause-launch').touch();birth=owner.call('GET','/fixture/birth')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(owner.call,'POST',owner.prefix+'/jobs:batch',{'commands':[v[0] for v in prepared],'max_concurrent':maximum})
                try:
                    wait_file(owner.root/'held.json');held=json.loads((owner.root/'held.json').read_text())
                    before=owner.call('GET',f'/fixture/rows/{owner.case_id}')
                    assert len(before)==3 and all(r['phase']=='queued' and not r['claim'] for r in before)
                    kill_owner(owner,birth)
                    try:future.result(timeout=10)
                    except (OSError,ValueError):pass
                    peer=new_api('delivery-batch-peer',url,shared(owner))
                    with peer:
                        peer.token=owner.token
                        rows=wait_rows(peer,owner.case_id,lambda rs:sum(r['phase']=='running' for r in rs)==1)
                        active=next(r['id'] for r in rows if r['phase']=='running')
                        queued=[r['id'] for r in rows if not r['claim']]
                        assert len(queued)==2
                        second=new_api('delivery-batch-peer2',url,shared(owner))
                        with second:
                            second.token=owner.token
                            second.call('POST',f'/jobs/{queued[-1]}:cancel')
                            for _ in range(2):second.call('POST','/fixture/recover')
                            rows=peer.call('GET',f'/fixture/rows/{owner.case_id}')
                            assert sum(r['claim'] for r in rows)==1
                            index=held['jobs'].index(active);prepared[index][2].touch()
                            peer.wait_job(active,timeout=45)
                            remaining=queued[0]
                            wait_file(prepared[held['jobs'].index(remaining)][1],seconds=35)
                            prepared[held['jobs'].index(remaining)][2].touch();peer.wait_job(remaining,timeout=45)
                            cancelled=prepared[held['jobs'].index(queued[-1])][1]
                            assert not cancelled.exists()
                            final=peer.call('GET',f'/cases/{owner.case_id}/job-batches/{held["batch_id"]}')
                            assert final['counts']['succeeded']==2 and final['counts']['cancelled']==1
                            for _,marker,_ in prepared:
                                if marker.exists():assert marker.read_text().splitlines()==['started']
                            receipts.append({'batch_maximum':maximum,'user_quota':quota,'before':before,'final':final})
                            passed(('batch' if maximum==1 else 'user')+'_capacity_survives_two_API_recovery_and_queued_cancellation')
                finally:
                    for _,_,finish in prepared:finish.touch()
                    (owner.root/'continue').touch()
    api=new_api('delivery-user-scope',url,paused=True)
    with api:
        api.create_case('Capacity across cases')
        api.call('POST','/fixture/limit',{'count':1})
        workspace=api.call('GET','/workspaces')[0]
        other=api.call('POST',f'/workspaces/{workspace["id"]}/cases',{'name':'Second case under same owner'})['id']
        prepared=[payload(api.root,'cross'+str(i)) for i in range(2)]
        ids=[api.call('POST',f'/fixture/seed/{case}',{'command':prepared[i][0]})['id'] for i,case in enumerate((api.case_id,other))]
        peer=new_api('delivery-user-peer',url,shared(api),paused=True)
        try:
            with peer:
                peer.token=api.token
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=[f.result(timeout=30)['result'] for f in [pool.submit(p.call,'POST',f'/fixture/deliver/{j}') for p,j in zip((api,peer),ids)]]
                assert sorted(results)==['delivery_deferred','started'],results
                selected=results.index('started');wait_file(prepared[selected][1])
                assert not prepared[1-selected][1].exists()
                prepared[selected][2].touch();api.wait_job(ids[selected],timeout=45)
                assert peer.call('POST',f'/fixture/deliver/{ids[1-selected]}')['result']=='started'
                prepared[1-selected][2].touch();api.wait_job(ids[1-selected],timeout=45)
                passed('different_case_claims_atomically_share_one_user_slot')
        finally:
            for _,_,finish in prepared:finish.touch()
    api=new_api('delivery-negative',url,paused=True)
    with api:
        api.create_case('No inferred delivery authority')
        command,marker,finish=payload(api.root,'untrusted');finish.touch()
        api.call('POST',f'/fixture/seed/{api.case_id}',{'command':command,'rollback':True})
        assert api.call('GET',f'/fixture/rows/{api.case_id}')==[]
        passed('rolled_back_intent_has_no_job_or_recoverable_work')
        old=api.call('POST',f'/fixture/seed/{api.case_id}',{'command':command,'intent':False})['id']
        bad=api.call('POST',f'/fixture/seed/{api.case_id}',{'command':command})['id']
        foreign=api.call('POST',f'/fixture/seed/{api.case_id}',{'command':command})['id']
        api.call('POST',f'/fixture/damage/{bad}',{'kind':'spec'})
        api.call('POST',f'/fixture/damage/{foreign}',{'kind':'host'})
        result=api.call('POST','/fixture/recover')
        assert str(old) not in result and result[str(bad)]=='evidence_invalid' and result[str(foreign)]=='delivery_deferred'
        rows=api.call('GET',f'/fixture/rows/{api.case_id}')
        assert all(r['phase']=='queued' and not r['claim'] and not r['identity'] for r in rows) and not marker.exists()
        passed('legacy_damaged_and_foreign_host_records_never_launch')
    return {'passed':True,'count':len(checks),'checks':checks,'receipts':receipts,
        'developer_database_touched':False,'real_runtime':['windows','wsl'] if wsl else ['windows']}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--wsl',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='delivery-recovery-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    results={'sqlite':await asyncio.to_thread(verify,wsl=args.wsl)}
    (report/'sqlite-result.json').write_text(json.dumps(results['sqlite'],indent=2),encoding='utf8')
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_delivery_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_delivery_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target,wsl=args.wsl)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('DELIVERY_RECOVERY_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
