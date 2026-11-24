"""Read-validation reservation protocol, real peer file APIs, and migration guards.

The fixture creates never-started drain checkpoints, then moves those synthetic
owners to finalizing directly. It does not simulate a native process exit.
This proves reservation/transaction semantics, not native-writer exclusion or
the still-pending integration with publication source/dependency validation.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request
import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic, engine_for


FIXTURE = '''import uuid
from dataclasses import replace
from datetime import timedelta
from fastapi import Depends
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Case,Job,JobAttempt,JobExecutionHead,CaseValidationFence
from app.routers.workspaces import get_owned_case
from app.services import case_validation as fence,job_execution
from app.services.case_write import coordinate
from app import storage
reservations={};owners={};offset=0
original_clock=fence.database_now
async def clock(db):return await original_clock(db)+timedelta(seconds=offset)
fence.database_now=clock

@app.post('/api/v1/fixture/clock')
async def clock_fault(seconds:int=0,user=Depends(get_current_user)):
 global offset
 offset=seconds
 return {'offset':offset}

@app.post('/api/v1/fixture/owner')
async def owner(case_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  job=Job(case_id=case.id,type='shell',status='running');db.add(job);await db.flush()
  attempt=JobAttempt(job_id=job.id,root_job_id=job.id,number=1,phase='claimed',claim_token=uuid.uuid4().hex)
  db.add(attempt);await db.flush()
  db.add(JobExecutionHead(job_id=job.id,active_attempt_id=attempt.id,next_number=2))
  await db.commit()
  owners[job.id]=job_execution.Claim(job.id,attempt.id,job.id,1,attempt.claim_token)
  from app.services.job_write_checkpoint import record_drained
  assert await record_drained(owners[job.id],None,not_started=True)
  # Protocol fixture only: production never transitions a claimed Job this way.
  attempt.phase='finalizing';await db.commit()
  return {'job_id':job.id}

async def load_owner(db,job_id):
 if job_id not in owners:
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  owners[job_id]=job_execution.Claim(job_id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
 return owners[job_id]

@app.post('/api/v1/fixture/op')
async def operation(job_id:int,action:str,rollback:bool=False,seconds:int=30,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  owner=await load_owner(db,job_id)
  job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
  if action=='reserve':
   claim=await fence.reserve_locked(db,owner,lease_seconds=seconds)
   reservations[job_id]=claim
   result={'generation':claim.generation}
  elif action=='renew':
   await fence.renew_locked(db,owner,reservations[job_id],lease_seconds=seconds);result={'ok':True}
  elif action in {'current','forged','wrong_generation','wrong_case','wrong_attempt'}:
   claim=reservations[job_id]
   if action=='forged':claim=replace(claim,token='wrong')
   if action=='wrong_generation':claim=replace(claim,generation=claim.generation+1)
   if action=='wrong_case':claim=replace(claim,case_id=claim.case_id+1)
   if action=='wrong_attempt':claim=replace(claim,attempt_id=str(uuid.uuid4()))
   await fence.require_current_locked(db,owner,claim);result={'ok':True}
  elif action in {'release','forged_release'}:
   claim=reservations[job_id]
   if action=='forged_release':claim=replace(claim,token='wrong')
   result={'released':await fence.release_locked(db,claim)}
  elif action=='revoke':
   selected_case_id=case.id
   await db.rollback()
   return {'revoked':await fence.revoke_expired(selected_case_id)}
  elif action=='heartbeat':
   await db.rollback()
   return {'status':await job_execution.heartbeat(owner)}
  elif action=='cancel':result={'status':await job_execution.request_cancel(db,job_id)}
  elif action=='change_owner':
   attempt=await db.get(JobAttempt,owner.attempt_id);attempt.claim_token=uuid.uuid4().hex;result={'changed':True}
  elif action=='write_rollback':
   async with coordinate(db,case):storage.put_text(case.s3_prefix,'probe.txt','AFTER-REVOKE')
   result={'written':True};rollback=True
  else:raise ValueError(action)
  if rollback:await db.rollback()
  else:await db.commit()
  return result

@app.post('/api/v1/fixture/constraints')
async def constraints(case_id:int,other_case:int,other_job:int,user=Depends(get_current_user)):
 checks=[]
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db);await get_owned_case(other_case,user,db)
  row=await db.get(CaseValidationFence,case_id)
  stamp=row.heartbeat_at
  patches={
   'generation_positive':{'generation':0},'known_status':{'status':'unknown'},
   'token_digest_length':{'token_sha256':'x'},'claim_digest_length':{'claim_sha256':'x'},
   'lease_after_heartbeat':{'lease_expires_at':stamp},
   'heartbeat_after_acquisition':{'heartbeat_at':row.acquired_at-timedelta(seconds=1)},
   'held_has_no_end':{'status':'held','ended_at':stamp},
   'released_has_end':{'status':'released','ended_at':None},
   'same_case_owner':{'case_id':other_case},'same_job_attempt':{'job_id':other_job},
   'attempt_exists':{'attempt_id':str(uuid.uuid4())}}
  for label,patch in patches.items():
   try:
    async with db.begin_nested():
     await db.execute(update(CaseValidationFence).where(CaseValidationFence.case_id==case_id).values(**patch))
     raise AssertionError('Missing constraint: '+label)
   except IntegrityError:checks.append(label)
  await db.rollback()
 return {'checks':checks}
'''


def request(api, path):
    req = urllib.request.Request(api.base+'/api/v1'+path, data=b'', method='POST',
        headers={'Authorization':'Bearer '+api.token})
    try: response = api.opener.open(req, timeout=15)
    except urllib.error.HTTPError as error: response = error
    with response: return response.status, json.loads(response.read())


def verify(database_url=None):
    api=IsolatedServer('case-validation',app='validation_fixture:app',database_url=database_url)
    (api.root/'validation_fixture.py').write_text(FIXTURE,encoding='utf8')
    if database_url is None:
        # Migrate exactly the harness-owned path; do not relax its URL guard.
        alembic('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix(),api.root,'upgrade','head')
    checks=[]
    with api:
        api.create_case('Validation protocol')
        workspace=api.call('GET',api.prefix)['workspace_id']
        other=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Unrelated case'})
        api.put('probe.txt','BASE')
        upload=api.call('POST',api.prefix+'/uploads',{'path':'upload.txt','size':3})
        peer=IsolatedServer('case-validation-peer',app='peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE,encoding='utf8')
        with peer:
            peer.token=api.token
            def owner():return api.call('POST',f'/fixture/owner?case_id={api.case_id}')['job_id']
            def op(job,action,expect=200,**kw):
                query=''.join('&'+key+'='+str(value).lower() for key,value in kw.items())
                return api.call('POST',f'/fixture/op?job_id={job}&action={action}'+query,expect=expect)
            def blocked():
                response=peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FORBIDDEN'},expect=409)
                assert response['detail']['code']=='validation_busy',response
                assert peer.call('GET',api.prefix+'/file?path=probe.txt')['content'] in {'BASE','AFTER-REVOKE'}
            job=owner()
            op(job,'reserve',rollback=True)
            peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'BASE'})
            checks.append('uncommitted_reservation_does_not_block')
            op(job,'reserve');blocked()
            checks.append('committed_reservation_blocks_peer_write_without_mutation')
            assert op(job,'revoke')['revoked'] is False
            op(job,'current');op(job,'renew');op(job,'heartbeat')
            checks.append('live_reservation_renews_and_job_heartbeat_commits')
            for suffix in ('/file?path=probe.txt','/file:version?path=probe.txt','/caseDict','/tree','/uploads/'+upload['id']):
                peer.call('GET',api.prefix+suffix)
            peer.call('GET',f'/jobs/{job}')
            checks.append('peer_file_dictionary_upload_and_job_reads_continue')
            peer.call('PUT',f"/cases/{other['id']}/file",{'path':'other.txt','content':'ALLOWED'})
            checks.append('unrelated_case_remains_writable')
            for action in ('forged','wrong_generation','wrong_case','wrong_attempt'):
                assert op(job,action,409)['detail']['code']=='validation_stale'
                checks.append(action+'_cannot_validate')
            assert op(job,'forged_release')['released'] is False
            blocked();checks.append('wrong_token_cannot_release')
            op(job,'release',rollback=True);blocked();op(job,'current')
            checks.append('rolled_back_release_retains_exclusion')
            second=owner()
            assert op(second,'reserve',409)['detail']['code']=='validation_busy'
            checks.append('second_attempt_cannot_replace_held_reservation')
            constraints=api.call('POST',f"/fixture/constraints?case_id={api.case_id}&other_case={other['id']}&other_job={second}")
            checks.extend('schema_'+label for label in constraints['checks'])
            api.call('POST','/fixture/clock?seconds=4000')
            for action in ('current','renew'):
                assert op(job,action,409)['detail']['code']=='validation_stale'
            blocked();checks.append('expired_held_row_still_blocks_writes')
            assert op(job,'revoke')['revoked'] is True
            op(job,'write_rollback')
            api.call('POST','/fixture/clock?seconds=0')
            assert peer.call('GET',api.prefix+'/file?path=probe.txt')['content']=='AFTER-REVOKE'
            assert op(job,'current',409)['detail']['code']=='validation_stale'
            assert op(job,'renew',409)['detail']['code']=='validation_stale'
            checks.append('committed_revocation_survives_write_rollback_and_clock_restore')
            assert op(second,'reserve')['generation']==2
            assert op(job,'release')['released'] is False
            assert op(job,'current',409)['detail']['code']=='validation_stale'
            blocked();checks.append('new_generation_rejects_old_cleanup_and_commit')
            op(second,'cancel')
            assert op(second,'current',409)['detail']['code']=='validation_stale'
            blocked();checks.append('cancellation_commits_without_unlocking_unfinished_validation')
            assert op(second,'release')['released'] is True
            assert op(second,'release')['released'] is False
            checks.append('exact_token_cleanup_after_cancel_is_idempotent')
            third=owner();op(third,'reserve');api.call('POST','/fixture/clock?seconds=-60')
            assert op(third,'renew',409)['detail']['code']=='validation_stale'
            assert op(third,'revoke')['revoked'] is True
            api.call('POST','/fixture/clock?seconds=0')
            assert op(third,'current',409)['detail']['code']=='validation_stale'
            checks.append('backward_clock_revocation_cannot_resurrect')
            fourth=owner();op(fourth,'reserve');op(fourth,'change_owner')
            assert op(fourth,'current',409)['detail']['code']=='state_conflict'
            assert op(fourth,'release')['released'] is True
            checks.append('changed_job_claim_cannot_validate_but_can_release_own_token')
            # The intentional owner corruption also invalidates that Job's drain
            # proof. Run the independent race on a fresh case, not a damaged one.
            api.create_case('Reservation race after ownership fault')
            peer.token=api.token
            fifth=owner()
            with ThreadPoolExecutor(2) as pool:
                path=f'/fixture/op?job_id={fifth}&action=reserve'
                futures=[pool.submit(request,which,path) for which in (api,peer)]
                responses=[future.result(timeout=20) for future in futures]
            assert sorted(code for code,_ in responses)==[200,409],responses
            winner=(api,peer)[next(i for i,(code,_) in enumerate(responses) if code==200)]
            winner.call('POST',f'/fixture/op?job_id={fifth}&action=release')
            checks.append('two_api_reservation_race_has_one_winner')
            peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FINAL'})
            assert peer.call('GET',api.prefix+'/file?path=probe.txt')['content']=='FINAL'
            checks.append('released_reservation_allows_peer_write')
        url=api.env['VIBEFLOW_DATABASE_URL']
    result={'checks':checks,'count':len(checks),'api_root':str(api.root),'peer_root':str(peer.root),
        'publication_integration':False,'native_writer_exclusion':False}
    (api.root/'validation-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result,url


async def migration_history(url,report):
    from app.models import CaseValidationFence
    engine=engine_for(url)
    try:
        async with engine.connect() as connection:
            before=[dict(row) for row in (await connection.execute(text('SELECT * FROM jobs ORDER BY id'))).mappings()]
            assert (await connection.execute(select(CaseValidationFence.case_id))).first()
        # Remove only this suite's synthetic checkpoint fixtures, then reach the
        # fence revision explicitly so the rejection exercises its own guard.
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE job_attempts SET write_checkpoint_json='{}'"))
        await asyncio.to_thread(alembic,url,report,'downgrade','5f3eb0a4d976')
        try: await asyncio.to_thread(alembic,url,report,'downgrade','4e2da9f3c865')
        except AssertionError: pass
        else: raise AssertionError('Downgrade discarded reservation history')
        async with engine.begin() as connection:
            assert (await connection.execute(text('SELECT version_num FROM alembic_version'))).scalar_one()=='5f3eb0a4d976'
            await connection.execute(delete(CaseValidationFence))
        await engine.dispose()
        await asyncio.to_thread(alembic,url,report,'downgrade','4e2da9f3c865')
        await asyncio.to_thread(alembic,url,report,'upgrade','head')
        await asyncio.to_thread(alembic,url,report,'check')
        async with engine.connect() as connection:
            after=[dict(row) for row in (await connection.execute(text('SELECT * FROM jobs ORDER BY id'))).mappings()]
            assert before==after
        return {'history_loss_rejected':True,'populated_predecessor_roundtrip':True,'metadata_diff':0}
    finally: await engine.dispose()


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='case-validation-evidence-',dir=SERVER.parents[1]/'tmp'))
    # Explicit migration also tests the new indexes/FKs on SQLite, not only create_all.
    result,sqlite=await asyncio.to_thread(verify)
    result['migration']=await migration_history(sqlite,report)
    results={'sqlite':result}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_validation_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_validation_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            result,_=await asyncio.to_thread(verify,target)
            result['migration']=await migration_history(target,report)
            results['postgresql']=result
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('CASE_VALIDATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
