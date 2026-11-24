"""Real delivery refusal for pre-isolation queued records; owned disposable API."""
import argparse
import json
from pathlib import Path
import sys

from testsupport import IsolatedServer
from default_workspace_smoke import command
from professional_launch_smoke import settled
from job_owner_startup_smoke import shared

FIXTURE = '''import os
os.environ.update(OVERRIDES)
import json,sys
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Job,JobAttempt,User,DurableEvent
from app.routers.auth import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_delivery,job_execution,job_recovery,distributed_queue
from app.services.durable_events import json_text,lock_case_execution
async def pause(stop):await stop.wait()
job_delivery.recover_forever=pause
@app.post('/api/v1/fixture/seed/{case_id}')
async def seed(case_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  spec={'command':data['command'],'runtime':'windows','argv':data.get('argv'),'case_prefix':case.s3_prefix,
   'timeout_seconds':30,'max_log_bytes':1048576,'evidence_run_dir':None}
  j,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
   digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec,
   input_paths=['Ref/input.txt'])
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id))
  value=json.loads(a.input_manifest_json)
  if data['mode']=='new_unsealed':
   value.pop('workspace');a.input_manifest_json=json_text(value)
  try:
   if data['mode'] not in {'old_redis','unverified_redis'}:
    await job_delivery.request(db,j,user_id=user.id,mode='redis' if data['mode']=='modern_redis' else 'local')
  except job_execution.ExecutionConflict as error:
   await db.rollback();return {'refused':error.code}
  if data['mode'] in {'old_unsealed','old_redis','old_argv'}:value.pop('workspace')
  if data['mode']=='wrong_binding':value['workspace']['source']['case_id']+=1
  a.input_manifest_json=json_text(value)
  await db.commit()
  return {'id':j.id,'attempt_id':a.id}
@app.post('/api/v1/fixture/deliver/{job_id}')
async def deliver(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id))
  spec=json.loads(a.input_manifest_json)['execution_spec']
 try:
  if data.get('path')=='redis':
   result=await distributed_queue.run_job({},job_id,spec['case_prefix'],data.get('command',spec['command']),spec['runtime'],
    timeout_seconds=spec['timeout_seconds'],max_log_bytes=spec['max_log_bytes'])
  elif data.get('path')=='claim':
   async with SessionLocal() as db:
    await job_execution.claim(db,job_id,spec);await db.commit()
   result='claimed'
  else:result=await job_delivery.deliver(job_id)
 except job_execution.ExecutionConflict as error:result=error.code
 return {'result':result}
@app.get('/api/v1/fixture/proof/{job_id}')
async def proof(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==j.id))
  events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id)))
  return {'status':j.status,'phase':a.phase,'claimed':a.claim_token is not None,
   'identity':a.process_identity_json,'notice_count':sum(e.type=='job.recovery_required' for e in events)}
'''


def api_for(overrides=None):
    api=IsolatedServer('unsealed-delivery',app='unsealed_fixture:app')
    (api.root/'unsealed_fixture.py').write_text(FIXTURE.replace('OVERRIDES',repr(overrides or {})),encoding='utf8')
    return api


def verify(settings=None):
    checks=[]
    with api_for(settings) as api:
        def case():
            api.create_case('Old delivery input isolation')
            api.put('Ref/input.txt','preserved original')
        code="from pathlib import Path; Path('unexpected-live-output.txt').write_text('executed')"
        payload=command(code)
        for mode in ('old_unsealed','old_argv','old_redis','wrong_binding'):
            case()
            body={'mode':mode,'command':payload}
            if mode=='old_argv':body['argv']=[sys.executable,'-c',code]
            created=api.call('POST',f'/fixture/seed/{api.case_id}',body)
            path='redis' if mode=='old_redis' else 'delivery'
            outcome=api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':path})
            assert outcome['result']=='input_recovery_required',(mode,outcome)
            proof=api.call('GET',f'/fixture/proof/{created["id"]}')
            assert proof=={'status':'queued','phase':'queued','claimed':False,'identity':'{}','notice_count':1},proof
            detail=api.call('GET',f'/jobs/{created["id"]}')
            assert detail['recovery']['reason']=='input_recovery_required'
            assert detail['recovery']['next_step']=='review_inputs_and_resubmit'
            assert api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':path})==outcome
            assert api.call('GET',f'/fixture/proof/{created["id"]}')==proof
            api.call('GET',api.prefix+'/raw?path=unexpected-live-output.txt',expect=404)
            assert api.call('GET',api.prefix+'/raw?path=Ref/input.txt',raw=True)==b'preserved original'
            if mode!='old_redis':
                refused=api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':'claim'})
                assert refused['result']=='recovery_required',refused
            with api_for({**(settings or {}),**shared(api)}) as peer:
                peer.token=api.token
                restored=peer.call('GET',f'/jobs/{created["id"]}')
                assert restored['recovery']['reason']=='input_recovery_required'
                assert peer.call('GET',f'/fixture/proof/{created["id"]}')==proof
            api.call('POST',f'/jobs/{created["id"]}:cancel')
            assert api.call('GET',f'/jobs/{created["id"]}')['status']=='cancelled'
            checks.append(mode+'_no_claim_no_native_write_durable_notice_restart_and_cancel')
            print('PASS '+checks[-1],flush=True)
        case()
        before=api.call('GET',api.prefix+'/jobs')
        refused=api.call('POST',f'/fixture/seed/{api.case_id}',{'mode':'new_unsealed','command':payload})
        assert refused=={'refused':'recovery_required'},refused
        assert api.call('GET',api.prefix+'/jobs')==before
        checks.append('new_delivery_intent_requires_sealed_inputs_and_rolls_back_job')
        created=api.call('POST',f'/fixture/seed/{api.case_id}',{'mode':'modern','command':payload})
        assert api.call('POST',f'/fixture/deliver/{created["id"]}',{})['result']=='started'
        done=settled(api,created['id']);assert done['status']=='succeeded',done
        run=done['attempt']['workspace']['run_path']
        assert api.call('GET',api.prefix+'/raw?path='+run+'/unexpected-live-output.txt',raw=True)==b'executed'
        api.call('GET',api.prefix+'/raw?path=unexpected-live-output.txt',expect=404)
        assert api.call('POST',f'/fixture/deliver/{created["id"]}',{})['result']=='already_owned_or_terminal'
        checks.append('sealed_modern_delivery_runs_once_in_private_directory')
        case()
        created=api.call('POST',f'/fixture/seed/{api.case_id}',{'mode':'unverified_redis','command':payload})
        assert api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':'redis'})['result']=='legacy_delivery_unverified'
        assert not api.call('GET',f'/fixture/proof/{created["id"]}')['claimed']
        api.call('GET',api.prefix+'/raw?path=unexpected-live-output.txt',expect=404)
        api.call('POST',f'/jobs/{created["id"]}:cancel')
        checks.append('old_redis_without_durable_authority_cannot_execute_even_with_sealed_files')
        case()
        for mode,changes in (('modern',{}),('modern_redis',{'command':'echo CHANGED_TRANSPORT_PAYLOAD'})):
            created=api.call('POST',f'/fixture/seed/{api.case_id}',{'mode':mode,'command':payload})
            refused=api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':'redis',**changes})
            assert refused['result']=='evidence_invalid',refused
            assert not api.call('GET',f'/fixture/proof/{created["id"]}')['claimed']
            api.call('POST',f'/jobs/{created["id"]}:cancel')
        checks.append('old_redis_rechecks_durable_payload_and_selected_delivery_mode')
        created=api.call('POST',f'/fixture/seed/{api.case_id}',{'mode':'modern_redis','command':payload})
        assert api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':'redis'})['result']=='started'
        done=settled(api,created['id']);assert done['status']=='succeeded',done
        run=done['attempt']['workspace']['run_path']
        assert api.call('GET',api.prefix+'/raw?path='+run+'/unexpected-live-output.txt',raw=True)==b'executed'
        api.call('GET',api.prefix+'/raw?path=unexpected-live-output.txt',expect=404)
        assert api.call('POST',f'/fixture/deliver/{created["id"]}',{'path':'redis'})['result']=='already_owned_or_terminal'
        checks.append('old_redis_can_deliver_matching_sealed_admitted_request_once')
        result={'passed':True,'backend':(settings or {}).get('VIBEFLOW_STORAGE_BACKEND','fs'),
            'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'unsealed-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print('UNSEALED_DELIVERY_OK '+json.dumps(result),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    if args.s3:
        from s3testsupport import ObjectStore
        from s3_typed_publication_smoke import RUNTIME
        with ObjectStore(executable=RUNTIME) as store:verify(store.settings())
    else:verify()
