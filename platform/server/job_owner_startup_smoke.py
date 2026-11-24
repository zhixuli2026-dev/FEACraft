"""New API startup preserves live workers; exact owner death never replays work.

Real Windows/WSL payloads and two APIs use disposable SQLite/PostgreSQL. Seeded
claim/registration/publication boundary rows are explicitly metadata fixtures.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
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
from professional_launch_smoke import settled
from job_release_smoke import wait_file
from app.services import process_identity_native as identity
from app.services.owned_process import wsl_path
from workflow_migration_smoke import alembic

FIXTURE = '''import os
os.environ.update(OVERRIDES)
os.environ['VIBEFLOW_JOB_LEASE_SECONDS']='12'
os.environ['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='3'
import json
from datetime import timedelta
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Job,JobAttempt,DurableEvent,User
from app.routers.auth import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_execution,job_recovery
from app.services.durable_events import json_text,now,lock_case_execution
@app.get('/api/v1/fixture/rows/{case_id}')
async def rows(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db)
  jobs=(await db.scalars(select(Job).where(Job.case_id==case_id).order_by(Job.id))).all()
  result=[]
  for job in jobs:
   a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
   events=(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id).order_by(DurableEvent.seq))).all() if a else []
   result.append({'id':job.id,'command':job.command,'status':job.status,'phase':a.phase if a else None,
     'worker':json.loads(a.worker_identity_json) if a else None,
     'heartbeat':a.heartbeat_at.isoformat() if a and a.heartbeat_at else None,
     'lease':a.lease_expires_at.isoformat() if a and a.lease_expires_at else None,
     'events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in events]})
  return result
@app.post('/api/v1/fixture/seed/{case_id}')
async def seed(case_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  label=data['label']
  if label=='legacy':
   job=Job(case_id=case_id,type='shell',command=label,status='running');db.add(job);await db.commit()
   return {'id':job.id}
  spec={'command':label,'case_prefix':case.s3_prefix,'runtime':'windows','argv':None,
        'timeout_seconds':30,'max_log_bytes':1024,'evidence_run_dir':None}
  job,_=await job_execution.submit(db,case_id=case_id,user_id=user.id,key=None,
   digest=job_execution.request_hash(spec),job_type='shell',command=label,spec=spec)
  if label!='unclaimed':
   owner=await job_execution.claim(db,job.id,spec)
   a=await db.get(JobAttempt,owner.attempt_id,populate_existing=True)
   value=json.loads(a.worker_identity_json)
   if label.endswith('_expired'):a.lease_expires_at=now()-timedelta(seconds=60)
   if label in {'starting_live','starting_dead'}:a.phase='starting'
   if label in {'finalizing_live','finalizing_dead'}:a.phase='finalizing';job.status='running'
   if label=='legacy_claim':value={}
   if label=='malformed':value={'schema':'wrong'}
   if label=='foreign_host':value['process']['host_id']='f'*64
   if label=='birth_mismatch':
    field='creation_time_100ns' if value['process']['platform']=='windows' else 'start_ticks'
    value['process'][field]=str(int(value['process'][field])+1)
   if label=='cross_attempt':value['attempt_id']=str(__import__('uuid').uuid4())
   if label.endswith('_dead'):value['process']=data['dead_birth']
   a.worker_identity_json=json_text(value)
  await db.commit()
  return {'id':job.id}
@app.post('/api/v1/fixture/reconcile')
async def reconcile(user:User=Depends(get_current_user)):
 return await job_recovery.reconcile_startup()
@app.get('/api/v1/fixture/startup')
async def startup_result(user:User=Depends(get_current_user)):
 return app.state.job_recovery
@app.get('/api/v1/fixture/identity')
async def process_identity(user:User=Depends(get_current_user)):
 from app.services.process_identity_native import observe
 return observe(os.getpid())
'''


def new_api(label, database_url, overrides=None):
    api = IsolatedServer(label, app='owner_fixture:app', database_url=database_url)
    (api.root/'owner_fixture.py').write_text(FIXTURE.replace('OVERRIDES', repr(overrides or {})), encoding='utf8')
    return api


def shared(api):
    return {key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET','VIBEFLOW_DATABASE_URL')}


def payload(api, runtime):
    marker, finish, script = api.root/'payload.started', api.root/'payload.finish', api.root/'payload.py'
    native = lambda path: str(path) if runtime=='windows' else wsl_path(path)
    script.write_text('import time\nfrom pathlib import Path\np=Path('+repr(native(marker))+')\n'
        'with p.open("a") as stream:stream.write("start\\n")\n'
        'deadline=time.monotonic()+60\n'
        'while not Path('+repr(native(finish))+').exists() and time.monotonic()<deadline:time.sleep(.02)\n', encoding='utf8')
    command = '"'+sys.executable+'" "'+str(script)+'"' if runtime=='windows' else 'python3 '+wsl_path(script)
    row = api.call('POST',api.prefix+'/jobs',{'command':command,'runtime':runtime})
    wait_file(marker)
    return row['id'],marker,finish


def check_settings():
    from app.config import Settings
    for lease,beat in ((9,1),(30,15),(10,5),(3601,10),(30,0)):
        try:Settings(_env_file=None,job_lease_seconds=lease,job_heartbeat_seconds=beat)
        except ValueError:pass
        else:raise AssertionError('Invalid lease timing accepted')
    assert Settings(_env_file=None,job_lease_seconds=12,job_heartbeat_seconds=3).job_lease_seconds==12


def verify(database_url=None):
    checks=[];receipts=[]
    check_settings();checks.append('configured_lease_and_heartbeat_bounds')
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-u','-c',
        'import sys;print("ready",flush=True);sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    assert sentinel.stdout.readline().strip()==b'ready'
    dead_birth=identity.observe(sentinel.pid)
    sentinel.communicate(b'x',timeout=10)
    assert identity.probe(dead_birth)['state']=='exited'
    api=new_api('job-owner-startup',database_url)
    with api:
        api.create_case('New API preserves existing worker')
        job_id,marker,finish=payload(api,'windows')
        before=api.call('GET',f'/jobs/{job_id}')
        first=api.call('GET',f'/fixture/rows/{api.case_id}')[0]
        from datetime import datetime
        assert first['worker']['process']==api.call('GET','/fixture/identity')
        assert identity.probe(first['worker']['process'])['state']=='match'
        assert 11.5 <= (datetime.fromisoformat(first['lease'])-datetime.fromisoformat(first['heartbeat'])).total_seconds() <= 12.5
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            renewed=api.call('GET',f'/fixture/rows/{api.case_id}')[0]
            if renewed['heartbeat']!=first['heartbeat']:break
            time.sleep(.1)
        else:raise AssertionError('Configured heartbeat did not renew the actual running job')
        assert renewed['worker']==first['worker']
        checks.append('actual_claim_birth_and_configured_heartbeat_renewal')
        labels=['unclaimed','legacy','claimed_live_expired','starting_live','finalizing_live',
                'legacy_claim','malformed','foreign_host','birth_mismatch','cross_attempt',
                'claimed_dead','starting_dead','finalizing_dead']
        ids={label:api.call('POST',f'/fixture/seed/{api.case_id}',{'label':label,'dead_birth':dead_birth})['id'] for label in labels}
        seeded=api.call('GET',f'/fixture/rows/{api.case_id}')
        peer=new_api('job-owner-peer',database_url,shared(api))
        with peer:
            peer.token=api.token
            after=peer.call('GET',f'/jobs/{job_id}')
            assert after['status']=='running' and after['attempt']['phase']=='running'
            assert identity.probe(first['worker']['process'])['state']=='match'
            counts=peer.call('GET','/fixture/startup')
            assert counts.get('owner_live')==4,counts
            checks.append('new_api_preserves_real_live_job_and_birth')
            rows=peer.call('GET',f'/fixture/rows/{api.case_id}')
            original={r['id']:r for r in seeded}
            byid={r['id']:r for r in rows}
            for label in labels:
                current=byid[ids[label]]
                if label=='claimed_dead':
                    assert current['status']==current['phase']=='interrupted'
                    assert len([e for e in current['events'] if e['type']=='attempt.owner_exited'])==1
                elif label=='starting_dead':
                    assert {k:v for k,v in current.items() if k!='events'}=={k:v for k,v in original[ids[label]].items() if k!='events'}
                    assert current['events'][:-1]==original[ids[label]]['events']
                    notice=current['events'][-1]
                    assert notice['type']=='attempt.registration_recovery_required'
                    assert notice['payload']['reason']=='legacy_registration' and not notice['payload']['replayed']
                elif label in {'unclaimed','legacy_claim','malformed','foreign_host','birth_mismatch','cross_attempt','finalizing_dead'}:
                    assert {k:v for k,v in current.items() if k!='events'}=={k:v for k,v in original[ids[label]].items() if k!='events'}
                    assert current['events'][:-1]==original[ids[label]]['events']
                    notice=current['events'][-1]
                    assert notice['type']=='job.recovery_required' and not notice['payload']['replayed']
                    assert not notice['payload']['stop_evidence_created']
                    assert peer.call('GET',f'/jobs/{ids[label]}')['recovery']['event_id']==notice['id']
                else:assert current==original[ids[label]],label
                checks.append(label+'_reconciled_without_replay_or_unverified_takeover')
            repeat=peer.call('POST','/fixture/reconcile')
            assert peer.call('GET',f'/fixture/rows/{api.case_id}')[1:]==rows[1:]
            checks.append('repeat_reconciliation_preserves_history')
            race=api.call('POST',f'/fixture/seed/{api.case_id}',{'label':'race_dead','dead_birth':dead_birth})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=[future.result(timeout=30) for future in [pool.submit(p.call,'POST','/fixture/reconcile') for p in (api,peer)]]
            assert sum(r.get('owner_exited_interrupted',0) for r in results)==1,results
            race_row=next(r for r in peer.call('GET',f'/fixture/rows/{api.case_id}') if r['id']==race['id'])
            assert len([e for e in race_row['events'] if e['type']=='attempt.owner_exited'])==1
            checks.append('two_reconcilers_commit_one_owner_exit_event')
            finish.touch();final=settled(api,job_id)
            assert final['status']=='succeeded' and final['attempt']['process_stop']['verified']
            assert marker.read_text().splitlines()==['start']
            checks.append('preserved_real_job_finishes_once_with_verified_cleanup')
            receipts.append({'startup':counts,'repeated':repeat,'concurrent':results,'before':before,'after':after,
                             'final':final,'seeded_before':seeded,'seeded_after':rows,'race':race_row,
                             'evidence':str(api.root),'peer_evidence':str(peer.root)})
    for runtime in ('windows','wsl'):
        owner=new_api('job-owner-death-'+runtime,database_url)
        with owner:
            owner.create_case('Exact owner death '+runtime)
            job_id,marker,_finish=payload(owner,runtime)
            row=owner.call('GET',f'/jobs/{job_id}')
            record=row['attempt']['process_identity'];owner_birth=owner.call('GET','/fixture/identity')
            owner.proc.kill();owner.proc.wait(timeout=10)
            deadline=time.monotonic()+10
            while identity.probe(owner_birth)['state']!='exited' and time.monotonic()<deadline:time.sleep(.05)
            assert identity.probe(owner_birth)['state']=='exited'
            deadline=time.monotonic()+15
            while identity.probe(record['host'])['state']!='exited' and time.monotonic()<deadline:time.sleep(.05)
            assert identity.probe(record['host'])['state']=='exited'
            peer=new_api('job-owner-death-reader',database_url,shared(owner))
            with peer:
                peer.token=owner.token
                after=peer.call('GET',f'/jobs/{job_id}')
                assert after['status']==after['attempt']['phase']=='interrupted',after
                assert after['attempt']['process_stop'] is None,'Recovery fabricated an actual stop receipt'
                observed=peer.call('GET',f'/jobs/{job_id}/process')
                assert observed['state']=='exited',observed
                peer.call('POST','/fixture/reconcile')
                assert peer.call('GET',f'/jobs/{job_id}')['attempt']==after['attempt']
                assert marker.read_text().splitlines()==['start']
                event_row=next(r for r in peer.call('GET',f'/fixture/rows/{owner.case_id}') if r['id']==job_id)
                assert len([e for e in event_row['events'] if e['type']=='attempt.owner_exited'])==1
                peer.create_case('Foreign recovery reader');peer.call('GET',f'/jobs/{job_id}',expect=404)
                receipts.append({'runtime':runtime,'job_before':row,'job_after':after,'observation':observed,
                                 'owner_birth':owner_birth,'events':event_row['events'],
                                 'evidence':str(owner.root),'peer_evidence':str(peer.root)})
                checks.append(runtime+'_actual_owner_death_audited_once_no_replay_or_forged_stop')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'real_payloads':['windows','wsl'],'metadata_boundary_fixtures':labels,
            'automatic_replay':False,'developer_database_touched':False}
    (api.root/'owner-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    (api.root/'owner-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='job-owner-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_owner_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_owner_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
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
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_OWNER_STARTUP_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
