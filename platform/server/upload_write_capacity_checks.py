"""Actual S3 upload promises, completion/cancel credit and shared ownership."""
import argparse
import asyncio
import hashlib
import json
import sys
import uuid
import httpx
from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
from app.models import Case,Workspace,Membership,UploadStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import uploads,s3_uploads,owner_storage_budget
quota_original=quota.quotas_for
async def configured_quota(db,user):
 value=await quota_original(db,user)
 path=root/'quota-limits.json'
 limits=json.loads(path.read_text()) if path.exists() else {}
 if str(user.id) in limits:value={**value,'storage_mb':limits[str(user.id)]}
 return value
quota.quotas_for=configured_quota
create=uploads.create_session
def creating(prefix,path,size,*args):
 pool=owner_storage_budget.current()
 assert pool is not None and pool.future_bytes==size
 result=create(prefix,path,size,*args)
 with (root/'writes.jsonl').open('a') as f:f.write(json.dumps({'operation':'create','owner':pool.ticket['user_id'],'future_bytes':size})+'\\n')
 return result
uploads.create_session=creating
complete=uploads.complete_session
async def completing(db,case,identifier):
 pool=owner_storage_budget.current();assert pool is not None
 result=await complete(db,case,identifier)
 with (root/'writes.jsonl').open('a') as f:f.write(json.dumps({'operation':'complete','owner':pool.ticket['user_id']})+'\\n')
 return result
uploads.complete_session=completing
cancel=uploads.cancel_session
def cancelling(*args):
 pool=owner_storage_budget.current();assert pool is not None
 result=cancel(*args)
 with (root/'writes.jsonl').open('a') as f:f.write(json.dumps({'operation':'cancel','owner':pool.ticket['user_id']})+'\\n')
 return result
uploads.cancel_session=cancelling
cleanup=s3_uploads.cleanup_completed
def cleaning(*args):
 assert owner_storage_budget.current() is not None
 if (root/'cleanup-failure').exists():raise OSError('intentional isolated cleanup failure')
 return cleanup(*args)
s3_uploads.cleanup_completed=cleaning
@app.get('/api/v1/fixture/uploads')
async def ledgers(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  rows=await db.scalars(select(UploadStorageReservation).where(UploadStorageReservation.user_id==user.id))
  return [{'id':r.id,'owner':r.user_id,'size':r.size,'remaining_bytes':r.remaining_bytes,'state':r.state} for r in rows]
@app.post('/api/v1/fixture/share/{case_id}/{member_id}')
async def share(case_id:int,member_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id)
  assert ws.user_id==user.id
  org=await quota.ensure_user_org(db,user)
  db.add(Membership(org_id=org.id,user_id=member_id,role='member'));await db.commit()
  return {'workspace_id':ws.id,'org_id':org.id}
'''


def verify(store,database_url=None,group='writes'):
    api=IsolatedServer('upload-write-capacity',app='upload_write_fixture:app',database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'upload_write_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE+OBSERVE,encoding='utf8')
    result={'passed':False,'database':'postgresql' if database_url else 'sqlite','llm_calls':0,'checks':[]}
    def save():(api.root/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    def check(name,**fields):
        result['checks'].append({'name':name,**fields});save();print('UPLOAD_WRITE_PASS '+name,flush=True)
    def credit():
        state=api.call('GET','/fixture/credit')
        assert state['rows'] and all(row['state']=='settled' and row['receipt']['closed'] for row in state['rows']),state
    def limits(value):(api.root/'quota-limits.json').write_text(json.dumps(value))
    def measured():return api.call('GET','/fixture/credit?measure=true')['bytes']
    print('UPLOAD_WRITE_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Upload write promises');owner=api.call('GET','/auth/me')['id']
            before=measured()
            limits({owner:0})
            api.call('POST',api.prefix+'/uploads',{'path':'Ref/denied.bin','size':32},expect=429)
            assert measured()==before and not api.call('GET','/fixture/uploads')
            credit();check('zero_quota_creation_no_remote_growth')
            limit_mb=before//(1024*1024)+1
            limits({owner:limit_mb})
            api.call('POST',api.prefix+'/uploads',{'path':'Ref/no-overhead.bin','size':limit_mb*1024*1024-before-1},expect=429)
            assert measured()==before and not api.call('GET','/fixture/uploads')
            credit();check('payload_cannot_consume_recovery_margin')
            size=limit_mb*1024*1024-before-150000
            assert size>0
            bounded=api.call('POST',api.prefix+'/uploads',{'path':'Ref/bounded.bin','size':size})
            ledgers=api.call('GET','/fixture/uploads')
            row=next(r for r in ledgers if r['id']==bounded['id'])
            assert row['remaining_bytes']==size and measured()+size<=limit_mb*1024*1024
            credit();check('creation_metadata_preserves_promised_payload',reserved=size,physical=measured(),limit=limit_mb*1024*1024)
            limits({})
            api.call('DELETE',api.prefix+'/uploads/'+bounded['id'])
            credit();check('cancel_releases_future_claim')
            payload=bytes(range(256))*128
            created=api.call('POST',api.prefix+'/uploads',{'path':'Ref/complete.bin','size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
            target=api.prefix+'/uploads/'+created['id']
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
                response=client.put(target+'?offset=0',content=payload);response.raise_for_status()
            before=measured();limits({owner:0})
            api.call('POST',target+':complete',{},expect=429)
            assert measured()==before
            credit();check('complete_refused_before_writes_when_quota_exhausted')
            limits({});(api.root/'cleanup-failure').touch()
            completed=api.call('POST',target+':complete',{})
            assert completed['cleanup_pending']
            credit();check('committed_upload_keeps_credit_through_cleanup_failure')
            (api.root/'cleanup-failure').unlink()
            replay=api.call('POST',target+':complete',{})
            assert not replay.get('cleanup_pending') and replay['sha256']==hashlib.sha256(payload).hexdigest()
            assert api.call('GET',api.prefix+'/raw?path=Ref/complete.bin',raw=True)==payload
            credit();check('completion_retry_cleans_actual_staging_and_preserves_bytes')
            cancelled=api.call('POST',api.prefix+'/uploads',{'path':'Ref/cancel.bin','size':64})
            path=api.prefix+'/uploads/'+cancelled['id'];before=measured()
            limits({owner:0});api.call('DELETE',path,expect=429)
            assert measured()==before
            credit();limits({})
            assert api.call('GET',path)['offset']==0
            api.call('DELETE',path);api.call('DELETE',path)
            assert next(r for r in api.call('GET','/fixture/uploads') if r['id']==cancelled['id'])['state']=='cancelled'
            credit();check('cancel_refusal_then_repeat_cancel_settles')
            auth=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Shared uploader'})
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+auth['token']},trust_env=False,timeout=60) as member:
                me=member.get('/auth/me');me.raise_for_status();member_id=me.json()['id']
                shared=api.call('POST',f'/fixture/share/{api.case_id}/{member_id}')
                api.call('PUT',f'/workspaces/{shared["workspace_id"]}/sharing',{'org_id':shared['org_id'],'role':'member','enabled':True})
                limits({member_id:0})
                response=member.post(api.prefix+'/uploads',json={'path':'Ref/shared.bin','size':len(payload)});response.raise_for_status()
                upload=response.json();target=api.prefix+'/uploads/'+upload['id']
                listed=member.get(api.prefix+'/uploads');listed.raise_for_status()
                assert any(r['id']==upload['id'] for r in listed.json())
                response=member.put(target+'?offset=0',content=payload);response.raise_for_status()
                response=member.post(target+':complete',json={});response.raise_for_status()
                assert api.call('GET',api.prefix+'/raw?path=Ref/shared.bin',raw=True)==payload
                row=next(r for r in api.call('GET','/fixture/uploads') if r['id']==upload['id'])
                assert row['owner']==owner and row['state']=='completed' and row['remaining_bytes']==0,row
                credit();check('shared_member_create_chunk_complete_and_list_bill_workspace_owner',owner=owner,member=member_id)
                limits({owner:0})
                response=member.post(api.prefix+'/uploads',json={'path':'Ref/owner-denied.bin','size':4})
                assert response.status_code==429,response.text
                credit();check('member_cannot_bypass_exhausted_owner_quota')
            limits({})
            writes=[json.loads(line) for line in (api.root/'writes.jsonl').read_text().splitlines()]
            assert writes and all(w['owner']==owner for w in writes)
            assert {'create','complete','cancel'}<=set(w['operation'] for w in writes)
            check('observed_write_scopes_all_bound_to_actual_owner',operations=len(writes))
            result['passed']=True;save()
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:2000]};save()
        print('UPLOAD_WRITE_FAILED '+json.dumps(result['failure']),flush=True);return 1
    print('UPLOAD_WRITE_OK '+str(api.root/'checks.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    args=parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
        code=asyncio.run(postgres(store,'writes',verify)) if args.database=='postgresql' else verify(store)
    sys.exit(code)
