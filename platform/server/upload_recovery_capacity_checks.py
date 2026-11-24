"""Actual upload bytes and sealed-input recovery with owner credit; no LLM."""
import argparse
import asyncio
import hashlib
import json
import sys
import time

import httpx
from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
import asyncio
import time
from app.models import Case,User,JobAttempt,UploadStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import job_execution,legacy_job_admission,attempt_seal_journal as seals,owner_storage_budget,uploads
from app.services.case_write import coordinate
from app.routers.files import upload_session_status
original_finish=seals.finish
def finish(*args,**kwargs):
 if (root/'observe-recovery').exists():
  assert owner_storage_budget.current() is not None
  with (root/'observed.jsonl').open('a') as f:f.write(json.dumps({'recovery_write_credit':True})+'\\n')
 return original_finish(*args,**kwargs)
seals.finish=finish
original_status=uploads.session_status
def status(*args,**kwargs):
 if (root/'hold-status').exists():
  (root/'status-ready').touch()
  deadline=time.monotonic()+20
  while not (root/'status-release').exists():
   if time.monotonic()>deadline:raise TimeoutError('status read barrier')
   time.sleep(.01)
 return original_status(*args,**kwargs)
uploads.session_status=status
@app.post('/api/v1/fixture/prepare/{case_id}')
async def prepare(case_id:int,committed:bool=False,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  async with coordinate(db,case):
   job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,digest='preparation-capacity',job_type='shell',command='echo PREPARED',
      spec=legacy_job_admission.execution_spec(case,'echo PREPARED'),input_paths=['Ref/source.txt'])
   attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
   result={'attempt_id':attempt.id,'job_id':job.id,'prefix':case.s3_prefix}
  if committed:await db.commit()
  else:await db.rollback()
 return result
@app.get('/api/v1/fixture/upload-ledger/{upload_id}')
async def ledger(upload_id:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  row=await db.get(UploadStorageReservation,upload_id)
  assert row.user_id==user.id
  return {'state':row.state,'remaining_bytes':row.remaining_bytes}
@app.post('/api/v1/fixture/cancel-status/{case_id}/{upload_id}')
async def cancel_status(case_id:int,upload_id:str,user=Depends(get_current_user)):
 async def read():
  async with SessionLocal() as db:
   selected=await db.get(User,user.id)
   return await upload_session_status(case_id,upload_id,selected,db)
 for name in ('status-ready','status-release'):(root/name).unlink(missing_ok=True)
 (root/'hold-status').touch()
 task=asyncio.create_task(read())
 try:
  deadline=asyncio.get_running_loop().time()+15
  while not (root/'status-ready').exists():
   if task.done():await task
   if asyncio.get_running_loop().time()>deadline:raise TimeoutError('status read did not enter')
   await asyncio.sleep(.02)
  task.cancel();await asyncio.sleep(.1)
  assert not task.done()
  async with SessionLocal() as db:
   active=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id,OwnerStorageReservation.state=='active')))
   assert active
  (root/'status-release').touch()
  try:await task
  except asyncio.CancelledError:pass
  else:raise AssertionError('cancelled status request returned success')
  return {'thread_drained_before_credit_release':True}
 finally:
  (root/'status-release').touch()
  (root/'hold-status').unlink(missing_ok=True)
  if not task.done():
   task.cancel()
   try:await task
   except asyncio.CancelledError:pass
'''


def verify(store,database_url=None,group='upload'):
    api=IsolatedServer('upload-recovery-capacity',app='upload_capacity_fixture:app',database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'upload_capacity_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+OBSERVE,encoding='utf8')
    result={'passed':False,'database':'postgresql' if database_url else 'sqlite','group':group,'llm_calls':0,'checks':[]}
    def save():(api.root/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def check(name,**fields):
        result['checks'].append({'name':name,**fields});save();print('UPLOAD_RECOVERY_PASS '+name,flush=True)
    def credit():
        state=api.call('GET','/fixture/credit')
        assert state['rows'] and all(row['state']=='settled' and row['receipt']['closed'] for row in state['rows']),state
    def objects(prefix):
        return {row['Key']:store.client.get_object(Bucket=store.bucket,Key=row['Key'])['Body'].read()
            for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket,Prefix=prefix)
            for row in page.get('Contents',[])}
    print('UPLOAD_RECOVERY_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Upload and recovery admission')
            endpoint=api.prefix+('/uploads/'+'0'*32 if group=='upload' else '/attempt-preparations:recover')
            method='GET' if group=='upload' else 'POST'
            before=api.call('GET','/fixture/credit?measure=true')['bytes']
            (api.root/'quota-zero').touch()
            try:
                api.call(method,endpoint,expect=429)
                assert api.call('GET','/fixture/credit?measure=true')['bytes']==before
                credit();check('zero_quota_refused_no_remote_growth')
            finally:(api.root/'quota-zero').unlink()
            if group=='upload':
                api.call('GET',endpoint,expect=404);credit();check('missing_status_settled')
                payload=bytes(range(256))*128
                created=api.call('POST',api.prefix+'/uploads',{'path':'Ref/upload.bin','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
                target=api.prefix+'/uploads/'+created['id']
                assert api.call('GET',target)['offset']==0
                credit();check('created_status_preserves_future_reservation',ledger=api.call('GET','/fixture/upload-ledger/'+created['id']))
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
                    sent=client.put(target+'?offset=0',content=payload[:16384]);sent.raise_for_status()
                    assert api.call('GET',target)['offset']==16384
                    assert api.call('GET','/fixture/upload-ledger/'+created['id'])['remaining_bytes']==16384
                    credit();check('partial_bytes_status_and_ledger')
                    assert api.call('POST',f'/fixture/cancel-status/{api.case_id}/{created["id"]}')['thread_drained_before_credit_release']
                    credit();check('cancelled_status_drains_thread_and_settles_credit')
                    sent=client.put(target+'?offset=16384',content=payload[16384:]);sent.raise_for_status()
                completed=api.call('POST',target+':complete',{})
                status=api.call('GET',target)
                assert status['id']==created['id'] and status['sha256']==hashlib.sha256(payload).hexdigest(),status
                assert api.call('GET',api.prefix+'/raw?path=Ref/upload.bin',raw=True)==payload
                assert api.call('GET','/fixture/upload-ledger/'+created['id'])=={'state':'completed','remaining_bytes':0}
                credit();check('completed_receipt_and_actual_bytes',id=completed['id'])
                cancelled=api.call('POST',api.prefix+'/uploads',{'path':'Ref/cancel.bin','size':128})
                cancelled_path=api.prefix+'/uploads/'+cancelled['id']
                api.call('DELETE',cancelled_path);api.call('GET',cancelled_path,expect=404)
                credit();check('cancelled_status_404_and_settled')
            else:
                assert api.call('POST',endpoint)['results']==[]
                credit();check('empty_recovery_settled')
                for mode in ('uncommitted','committed','foreign'):
                    api.create_case('Preparation '+mode);api.put('Ref/source.txt','ACTUAL SEALED INPUT')
                    prepared=api.call('POST',f'/fixture/prepare/{api.case_id}?committed='+str(mode=='committed').lower())
                    prefix=prepared['prefix'];identifier=prepared['attempt_id']
                    pending=prefix+'/.vibeflow/attempt-provisions/pending/'+identifier+'.json'
                    base=prefix+'/.vibeflow/attempts/'+identifier
                    if mode=='foreign':store.client.put_object(Bucket=store.bucket,Key=base+'/FOREIGN.txt',Body=b'UNOWNED RETAIN')
                    prior=objects(prefix+'/');assert pending in prior
                    endpoint=api.prefix+'/attempt-preparations'
                    diagnosis=api.call('GET',endpoint)
                    assert any(row.get('attempt_id')==identifier for row in diagnosis['results']),diagnosis
                    (api.root/'observe-recovery').touch()
                    try:recovered=api.call('POST',endpoint+':recover')['results']
                    finally:(api.root/'observe-recovery').unlink()
                    expected='committed' if mode=='committed' else 'recovery_required' if mode=='foreign' else 'rolled_back'
                    assert len(recovered)==1 and recovered[0]['status']==expected,recovered
                    after=objects(prefix+'/')
                    assert after[prefix+'/Ref/source.txt']==b'ACTUAL SEALED INPUT'
                    if mode=='foreign':
                        assert after==prior
                    else:
                        assert pending not in after
                        receipt=json.loads(after[prefix+'/.vibeflow/attempt-provisions/history/'+identifier+'.json'])
                        assert receipt['outcome']==expected
                        if mode=='committed':assert after[base+'/inputs/Ref/source.txt']==b'ACTUAL SEALED INPUT'
                        else:assert not any(k.startswith(base+'/') for k in after)
                        assert api.call('POST',endpoint+':recover')['results']==[]
                    credit();check(mode+'_actual_seal_recovery',attempt_id=identifier,status=expected)
                observed=[json.loads(line) for line in (api.root/'observed.jsonl').read_text().splitlines()]
                assert len(observed)==2,observed
                check('all_recovery_history_writes_held_owner_credit',writes=len(observed))
            result['passed']=True;save()
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:2000]};save()
        print('UPLOAD_RECOVERY_FAILED '+json.dumps(result['failure']),flush=True);return 1
    print('UPLOAD_RECOVERY_OK '+str(api.root/'checks.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    parser.add_argument('--group',choices=('upload','recovery'),default='upload')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        code=asyncio.run(postgres(store,args.group,verify)) if args.database=='postgresql' else verify(store,group=args.group)
    sys.exit(code)
