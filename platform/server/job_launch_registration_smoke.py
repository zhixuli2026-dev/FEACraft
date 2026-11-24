"""Actual startup commit faults and API deaths before payload release, isolated DBs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from process_registration_smoke import CONTROLLED
from job_release_smoke import wait_file
from job_owner_startup_smoke import shared
from app.services import process_identity_native as identity
from app.services.owned_process import wsl_path
from workflow_migration_smoke import alembic

EXTRA = '''
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.models import User,Job,JobAttempt,DurableEvent
from app.routers.auth import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_launch_registration as launches,job_recovery
from app.services import process_record
from app.services.owned_process import OwnedProcess
from app.services.process_identity_native import observe
import os
async def barrier(name):
 (root/(name+'.ready')).touch()
 for _ in range(2000):
  if (root/(name+'.continue')).exists():return
  await asyncio.sleep(.02)
 raise TimeoutError(name)
reserve_original=launches.reserve
async def reserve(owner,process):
 (root/'job-id').write_text(str(owner.job_id))
 await reserve_original(owner,process)
 if (root/'mode').read_text()=='duplicate_intent':await reserve_original(owner,process)
 if (root/'mode').read_text()=='intent_hold':await barrier('intent')
launches.reserve=reserve
prepared_original=launches.prepared
async def prepared(owner,process):
 await prepared_original(owner,process)
 (root/'launch.json').write_text(json.dumps(process.record))
 if (root/'mode').read_text()=='prepared_hold':await barrier('prepared')
launches.prepared=prepared
inspect_original=process_record.inspect_record
async def inspect_record(record):
 result=await inspect_original(record)
 if (root/'mode').read_text()=='probe_hold':await barrier('probe')
 return result
process_record.inspect_record=inspect_record
from contextvars import ContextVar
notice_commit=ContextVar('notice_commit',default=False)
notice_event_original=job_execution.attempt_event
async def notice_event(db,job,attempt,kind,payload):
 result=await notice_event_original(db,job,attempt,kind,payload)
 if kind=='attempt.registration_recovery_required':db.info['notice_commit']=True
 if kind in {launches.INTENT,launches.PREPARED}:db.info['launch_commit']='intent' if kind==launches.INTENT else 'prepared'
 return result
job_execution.attempt_event=notice_event
notice_commit_original=AsyncSession.commit
async def notice_commit_wrapper(self):
 component=self.info.pop('launch_commit',None)
 mode=(root/'mode').read_text()
 if component and mode==component+'_before':raise OSError('Injected launch pre-commit failure')
 fault=root/'notice.commit'
 selected=self.info.pop('notice_commit',False) and fault.exists()
 if selected and fault.read_text()=='before':raise OSError('Injected registration audit pre-commit failure')
 result=await notice_commit_original(self)
 if component and mode==component+'_after':raise OSError('Injected launch acknowledgement loss')
 if selected and fault.read_text()=='after':raise OSError('Injected registration audit acknowledgement loss')
 return result
AsyncSession.commit=notice_commit_wrapper
prepare_process_original=OwnedProcess.prepare
async def prepare_process(self,*args,**kwargs):
 (root/'native.prepare.entered').touch()
 return await prepare_process_original(self,*args,**kwargs)
OwnedProcess.prepare=prepare_process
saved_mutations={}
@app.post('/api/v1/fixture/mutate/{job_id}/{field}')
async def mutate(job_id:int,field:str,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  if field=='prepared_history':
   row=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==a.id,DurableEvent.type==launches.PREPARED))
   attribute='payload_json';new_value=json.dumps({'identity':{}})
  elif field=='notice_history':
   row=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==a.id,
    DurableEvent.type=='attempt.registration_recovery_required').order_by(DurableEvent.seq.desc()).limit(1))
   attribute='payload_json';new_value='[]'
  elif field in {'claim_token','input_manifest_json','process_identity_json'}:
   row=a;attribute=field;new_value='changed-fixture-claim' if field=='claim_token' else '{"fixture":"changed"}'
  else:raise HTTPException(400,'Unknown fixture field')
  key=(job_id,field)
  if key in saved_mutations:setattr(row,attribute,saved_mutations.pop(key))
  else:saved_mutations[key]=getattr(row,attribute);setattr(row,attribute,new_value)
  await db.commit()
  return {'ok':True}
@app.get('/api/v1/fixture/identity')
async def own_identity(user:User=Depends(get_current_user)):return observe(os.getpid())
@app.post('/api/v1/fixture/reconcile')
async def reconcile(user:User=Depends(get_current_user)):return await job_recovery.reconcile_startup()
@app.get('/api/v1/fixture/history/{job_id}')
async def history(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id).order_by(DurableEvent.seq)))
  return {'checkpoint':json.loads(a.write_checkpoint_json),'phase':a.phase,'identity':json.loads(a.process_identity_json),
    'events':[{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json)} for e in events]}
@app.post('/api/v1/fixture/reclaim/{job_id}')
async def reclaim(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  try:await job_execution.claim(db,job_id,json.loads(a.input_manifest_json)['execution_spec'])
  except job_execution.ExecutionConflict as error:raise HTTPException(409,error.code)
  raise AssertionError('An unresolved launch was claimed again')
'''


def new_api(label, database_url=None, overrides=None):
    api=IsolatedServer(label,app='launch_fixture:app',database_url=database_url)
    fixture='import os\nos.environ.update('+repr(overrides or {})+')\n'+CONTROLLED+EXTRA
    (api.root/'launch_fixture.py').write_text(fixture,encoding='utf8')
    (api.root/'mode').write_text('')
    return api


def verify(database_url=None, *, wsl=False):
    startup=verify_startup_commits(database_url)
    checks=list(startup['checks']);receipts=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    runtimes=['windows']+(['wsl'] if wsl else [])
    api=new_api('launch-registration',database_url)
    with api:
        for runtime in runtimes:
            for mode in ('fail_commit','cancel_before_registration','cancel_after_registration','bad_birth','bad_hash'):
                api.create_case(runtime+' '+mode)
                marker=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))/'payload.txt'
                (api.root/'mode').write_text(mode)
                expected=500 if mode.startswith('cancel_') else 409
                api.call('POST',api.prefix+'/jobs',{'command':'echo UNRELEASED>payload.txt','runtime':runtime},expect=expected,raw=expected==500)
                job_id=int((api.root/'job-id').read_text())
                row=api.wait_job(job_id,expected='cancelled' if mode.startswith('cancel_') else 'failed')
                stop=row['attempt']['process_stop'];record=row['attempt']['process_identity']
                assert stop and stop['verified'] and record and not marker.exists(),row
                assert row['attempt']['registration']['stage']=='aborted'
                assert api.call('GET',f'/jobs/{job_id}/process')['state']=='exited'
                history=api.call('GET',f'/fixture/history/{job_id}')
                kinds=[e['type'] for e in history['events']]
                for kind in ('attempt.launch_intended','attempt.launch_prepared','attempt.launch_aborted','attempt.process_stopped'):
                    assert kinds.count(kind)==1,(mode,kinds)
                assert history['checkpoint']['state']=='stopped'
                aborted=next(e['payload'] for e in history['events'] if e['type']=='attempt.launch_aborted')
                assert aborted['running_registration_committed']==(mode=='cancel_after_registration')
                assert aborted['stop']==stop and aborted['identity']==record
                receipts.append({'runtime':runtime,'mode':mode,'job':row,'history':history})
                passed(runtime+'_'+mode+'_keeps_actual_cleanup_without_payload_release')
        # A normal launch still passes the same intent/preparation/running sequence.
        (api.root/'mode').write_text('')
        api.create_case('Normal registered launch')
        job=api.call('POST',api.prefix+'/jobs',{'command':'echo NORMAL','runtime':'windows'})
        final=api.wait_job(job['id']);assert final['attempt']['registration']['stage']=='registered'
        assert final['attempt']['process_stop']['verified']
        passed('normal_registered_execution_and_cleanup_still_succeed')
        (api.root/'receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    for runtime in runtimes:
        for boundary in ('intent','prepared'):
            owner=new_api('launch-death-'+boundary+'-'+runtime,database_url)
            with owner:
                owner.create_case('Owner death at '+boundary+' '+runtime)
                marker=next((owner.root/'files/workspaces').glob(f'*/cases/{owner.case_id}'))/'payload.txt'
                (owner.root/'mode').write_text(boundary+'_hold')
                birth=owner.call('GET','/fixture/identity')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    submitted=pool.submit(owner.call,'POST',owner.prefix+'/jobs',{'command':'echo UNRELEASED>payload.txt','runtime':runtime})
                    try:
                        wait_file(owner.root/(boundary+'.ready'))
                        job_id=int((owner.root/'job-id').read_text())
                        initial=owner.call('GET',f'/jobs/{job_id}')
                        assert initial['attempt']['phase']=='starting'
                        assert initial['attempt']['registration']['stage']==('intended' if boundary=='intent' else 'prepared')
                        with new_api('launch-live-peer',database_url,shared(owner)) as peer:
                            peer.token=owner.token
                            live=peer.call('GET',f'/jobs/{job_id}')
                            assert live['attempt']==initial['attempt'] and not marker.exists()
                            passed(runtime+'_'+boundary+'_peer_preserves_live_starting_owner')
                            owner.proc.kill();owner.proc.wait(timeout=10)
                            end=time.monotonic()+15
                            while identity.probe(birth)['state']!='exited' and time.monotonic()<end:time.sleep(.05)
                            assert identity.probe(birth)['state']=='exited'
                            try:submitted.result(timeout=15)
                            except (OSError,ValueError):pass
                            else:raise AssertionError('Dead API unexpectedly acknowledged launch')
                            fault='before' if boundary=='intent' else 'after'
                            (peer.root/'notice.commit').write_text(fault)
                            try:peer.call('POST','/fixture/reconcile',expect=500,raw=True)
                            finally:(peer.root/'notice.commit').unlink()
                            committed=peer.call('GET',f'/fixture/history/{job_id}')
                            assert sum(e['type']=='attempt.registration_recovery_required' for e in committed['events'])==(0 if fault=='before' else 1)
                            passed(runtime+'_'+boundary+'_audit_'+fault+'_commit_fault_preserves_transaction_outcome')
                            with new_api('launch-restarted',database_url,shared(owner)) as restarted:
                                restarted.token=owner.token
                                after=restarted.call('GET',f'/jobs/{job_id}')
                                reg=after['attempt']['registration'];notice=reg['recovery']
                                assert after['attempt']['phase']=='starting' and after['attempt']['process_stop'] is None
                                assert notice['state']=='requires_attention' and notice['automatic_restart_allowed'] is False
                                assert notice['owner_state']=='exited'
                                if boundary=='prepared':assert notice['process_state']=='exited',notice
                                assert not marker.exists()
                                jobs=[pool.submit(p.call,'POST','/fixture/reconcile') for p in (peer,restarted)]
                                for future in jobs:future.result(timeout=20)
                                again=peer.call('GET',f'/fixture/history/{job_id}')
                                notices=[e for e in again['events'] if e['type']=='attempt.registration_recovery_required']
                                assert len(notices)==1,notices
                                assert again['checkpoint']=={} and again['identity']=={}
                                if runtime=='windows' and boundary=='prepared':
                                    for field in ('claim_token','input_manifest_json','process_identity_json','prepared_history'):
                                        for suffix in ('ready','continue'):(peer.root/('probe.'+suffix)).unlink(missing_ok=True)
                                        (peer.root/'mode').write_text('probe_hold')
                                        pending=pool.submit(peer.call,'POST','/fixture/reconcile')
                                        try:
                                            wait_file(peer.root/'probe.ready')
                                            start=time.monotonic()
                                            restarted.call('POST',f'/fixture/mutate/{job_id}/{field}')
                                            assert time.monotonic()-start<5,'Observation retained the case transaction'
                                        finally:
                                            (peer.root/'mode').write_text('')
                                            (peer.root/'probe.continue').touch()
                                        outcome=pending.result(timeout=20)
                                        assert outcome.get('registration_changed')==1,outcome
                                        restarted.call('POST',f'/fixture/mutate/{job_id}/{field}')
                                        assert peer.call('GET',f'/fixture/history/{job_id}')==again
                                        passed('unlocked_registration_observation_rechecks_'+field)
                                peer.call('POST',f'/fixture/reclaim/{job_id}',expect=409)
                                assert peer.call('GET',f'/fixture/history/{job_id}')==again
                                passed(runtime+'_'+boundary+'_death_is_audited_once_without_reclaim_or_forged_cleanup')
                                if runtime=='windows' and boundary=='prepared':
                                    restarted.call('POST',f'/fixture/mutate/{job_id}/notice_history')
                                    result=peer.call('POST','/fixture/reconcile')
                                    assert result.get('registration_recovery_required',0)>=1,result
                                    repaired=peer.call('GET',f'/fixture/history/{job_id}')
                                    recorded=[e for e in repaired['events'] if e['type']=='attempt.registration_recovery_required']
                                    assert len(recorded)==2 and recorded[0]['payload']==[]
                                    assert recorded[1]['payload']['schema']=='vibeflow.registration-recovery.v1'
                                    assert not recorded[1]['payload']['replayed']
                                    passed('malformed_old_notice_is_preserved_without_breaking_reconciliation')
                                token=restarted.token
                                restarted.create_case('Other registration reader')
                                restarted.call('GET',f'/jobs/{job_id}',expect=404)
                                restarted.token=token
                                passed(runtime+'_'+boundary+'_recovery_detail_enforces_case_ownership')
                                (owner.root/'death-result.json').write_text(json.dumps({'before':initial,'after':after,
                                    'history':again,'owner_birth':birth,'payload_released':False},indent=2),encoding='utf8')
                    finally:
                        (owner.root/(boundary+'.continue')).touch()
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
        'actual_runtimes':runtimes,'developer_database_touched':False}
    (api.root/'launch-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def verify_startup_commits(database_url=None, **_kwargs):
    api=new_api('launch-commit-faults',database_url);checks=[];receipts=[]
    with api:
        for mode in ('intent_before','intent_after','prepared_before','prepared_after','duplicate_intent'):
            api.create_case(mode)
            (api.root/'mode').write_text(mode)
            (api.root/'native.prepare.entered').unlink(missing_ok=True)
            (api.root/'launch.json').unlink(missing_ok=True)
            marker=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))/'payload.txt'
            api.call('POST',api.prefix+'/jobs',{'command':'echo UNRELEASED>payload.txt','runtime':'windows'},expect=409)
            job_id=int((api.root/'job-id').read_text())
            row=api.wait_job(job_id,expected='failed')
            state=api.call('GET',f'/fixture/history/{job_id}')
            kinds=[e['type'] for e in state['events']]
            assert not marker.exists()
            assert kinds.count('attempt.launch_intended')==(0 if mode=='intent_before' else 1)
            if mode.startswith('prepared_'):
                assert (api.root/'native.prepare.entered').exists()
                assert row['attempt']['process_stop']['verified'] and row['attempt']['registration']['stage']=='aborted'
                assert state['checkpoint']['state']=='stopped'
                assert kinds.count('attempt.launch_prepared')==(1 if mode=='prepared_after' else 0)
                assert kinds.count('attempt.launch_aborted')==1
                assert api.call('GET',f'/jobs/{job_id}/process')['state']=='exited'
            else:
                assert not (api.root/'native.prepare.entered').exists()
                assert row['attempt']['process_identity'] is None and row['attempt']['process_stop'] is None
                assert state['checkpoint']=={} and 'attempt.launch_prepared' not in kinds
            checks.append(mode+'_preserves_committed_launch_history_without_releasing_payload')
            print('PASS '+checks[-1],flush=True);receipts.append({'mode':mode,'job':row,'history':state})
        (api.root/'commit-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'commit-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--wsl',action='store_true')
    parser.add_argument('--startup-commits-only',action='store_true')
    args=parser.parse_args();report=Path(tempfile.mkdtemp(prefix='launch-registration-evidence-',dir=SERVER.parents[1]/'tmp'))
    runner=verify_startup_commits if args.startup_commits_only else verify
    results={'sqlite':await asyncio.to_thread(runner,wsl=args.wsl)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_launch_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_launch_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(runner,target,wsl=args.wsl)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('JOB_LAUNCH_REGISTRATION_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
