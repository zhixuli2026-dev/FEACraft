"""Actual case-scoped S3 PUT, lost acknowledgement, and rotating recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import uuid


FIXTURE = '''
import asyncio
import hashlib
import os
import time
import uuid
from datetime import datetime,timedelta,timezone
from fastapi import Depends
from sqlalchemy import select
from botocore.client import BaseClient
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import StorageWriteReservation
from app.routers.workspaces import get_owned_case
from app.services import remote_write_budget as budget, storage_operations as operations
from app.services.durable_events import json_text

budget.RECONCILE_PAGE_SIZE = 2
(root/'case-reservation-pid').write_text(str(os.getpid()))
raw_call = BaseClient._make_api_call

def lose_reply(client,operation,arguments):
    response = raw_call(client,operation,arguments)
    if operation == 'PutObject' and arguments['Key'].endswith('/lost-reply.bin'):
        (root/'actual-put-accepted').write_text(response['ETag'])
        raise TimeoutError('Test discarded the successful S3 response')
    return response
BaseClient._make_api_call = lose_reply

read_proof = budget.completed_immutable
def delayed_proof(ticket):
    if (root/'pause-proof').exists() and 'lost-reply.bin' in ticket['request_json'] and 'b'*64 not in ticket['request_json']:
        (root/'proof-entered').touch()
        end = time.monotonic() + 25
        while not (root/'proof-release').exists():
            if time.monotonic() >= end:
                raise RuntimeError('Test did not release the proof reader')
            time.sleep(.02)
    return read_proof(ticket)
budget.completed_immutable = delayed_proof

def checked(case_id,user):
    async def verify(db):
        return await get_owned_case(case_id,user,db)
    return verify

@app.post('/api/v1/cases/{case_id}/testing/case-reservations')
async def seed(case_id:int,user=Depends(get_current_user)):
    async with operations.hold(checked(case_id,user),purpose='staging') as lease:
        prefix = lease.case.s3_prefix
        bucket = get_settings().s3_bucket
        scope = storage.s3().raw
        object_prefix = prefix.rstrip('/')+'/.vibeflow/live-store/objects/'+str(uuid.uuid4())+'/'
        target = object_prefix+'lost-reply.bin'
        actual = b'verify the actual successful remote PUT'
        original = {'schema':'vibeflow.remote-write.v1','bucket':bucket,'key':prefix.rstrip('/')+'/.vibeflow/live-store/head.json',
                    'size':64,'sha256':'a'*64,'if_match':'"pending-head"','if_none_match':None}
        kinds = {'mutable':json_text(original),'malformed':'[]',
                 'wrong_digest':json_text({**original,'key':target,'sha256':'b'*64,'size':len(actual),
                                           'if_none_match':'*','if_match':None})}
        ids = {}
        async with lease.control() as db:
            case = await get_owned_case(case_id,user,db)
            for offset,(kind,request) in enumerate(kinds.items()):
                at = datetime(2020,1,1,tzinfo=timezone.utc) + timedelta(seconds=offset)
                size = len(actual) if kind == 'wrong_digest' else 64
                ids[kind] = str(uuid.uuid4())
                db.add(StorageWriteReservation(id=ids[kind],operation_id=lease.id,user_id=user.id,case_id=case_id,
                    binding_json=budget.binding(case),request_json=request,writer_json=lease.owner_json,
                    size=size,remaining_bytes=size,state='active',receipt_json='{}',created_at=at,updated_at=at))
        try:
            await asyncio.to_thread(scope.put_object,Bucket=bucket,Key=target,Body=actual,IfNoneMatch='*')
        except TimeoutError:
            assert (root/'actual-put-accepted').exists()
        else:
            raise AssertionError('Expected the fixture to lose an actual acknowledgement')
        async with lease.control() as db:
            reservations = await db.scalars(select(StorageWriteReservation).where(StorageWriteReservation.operation_id==lease.id))
            ids['materialized'], = [row.id for row in reservations if 'lost-reply.bin' in row.request_json and row.id not in ids.values()]
        return {'rows':ids,'key':target,'size':len(actual),'sha256':hashlib.sha256(actual).hexdigest()}

@app.get('/api/v1/cases/{case_id}/testing/case-reservations')
async def status(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    rows = await db.scalars(select(StorageWriteReservation).where(StorageWriteReservation.case_id==case_id))
    return {row.id:{'state':row.state,'bytes':row.remaining_bytes,'receipt':json.loads(row.receipt_json),
                   'updated_at':row.updated_at.isoformat()} for row in rows}

@app.post('/api/v1/cases/{case_id}/testing/case-reservations:review')
async def review(case_id:int,user=Depends(get_current_user)):
    async with operations.hold(checked(case_id,user),purpose='recovery') as lease:
        lease.resolved = True
    return {'ok':True}
'''


def scenario(api, *, store=None, **_):
    import hashlib
    from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
    from live_llm_job_repeat_acceptance import write
    from live_llm_outbox_restart_acceptance import restart

    assert store is not None
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    assert store.raw_client.get_bucket_versioning(Bucket=store.bucket)['Status']=='Enabled'
    ws = api.call('GET','/workspaces')[0]['id']
    other = write(api,'POST',f'/workspaces/{ws}/cases',{'name':'Parallel case during verification'})['id']
    other_case = f'/cases/{other}'
    path = api.prefix+'/testing/case-reservations'
    seeded = api.call('POST',path)

    def snapshot():
        rows = api.call('GET',path)
        return {kind:rows[identifier] for kind,identifier in seeded['rows'].items()}

    physical = store.raw_client.get_object(Bucket=store.bucket,Key=seeded['key'])
    original_etag = (api.root/'actual-put-accepted').read_text()
    body = physical['Body']
    try:
        assert physical['ETag']==original_etag and physical.get('VersionId')
        assert hashlib.sha256(body.read()).hexdigest()==seeded['sha256']
    finally:
        body.close()
    start = snapshot()
    assert all(row['state']=='active' and row['receipt']=={} for row in start.values())
    guest = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Unrelated account',
        'password':'isolated-reservation-123'})
    api.call('POST',path+':review',expect=404,extra_headers={'Authorization':'Bearer '+guest['token']})
    assert snapshot()==start

    api.call('POST',path+':review')
    first = snapshot()
    assert first['mutable']['updated_at']>start['mutable']['updated_at']
    assert first['malformed']['updated_at']>start['malformed']['updated_at']
    assert first['wrong_digest']==start['wrong_digest'] and first['materialized']==start['materialized']

    kill_owned_api(api,int((api.root/'case-reservation-pid').read_text()))
    (api.root/'pause-proof').touch()
    restart(api)
    with ThreadPoolExecutor(1) as pool:
        review = pool.submit(api.call,'POST',path+':review')
        try:
            wait_for((api.root/'proof-entered').exists,'Later proof did not enter the actual S3 read',20)
            assert not review.done()
            api.call('POST',path+':review',expect=409)
            write(api,'PUT',other_case+'/file',{'path':'Ref/concurrent.txt','content':'same owner, different case'})
        finally:
            (api.root/'proof-release').touch()
        assert review.result(timeout=22)['ok']

    final = snapshot()
    for kind in ('mutable','malformed','wrong_digest'):
        assert final[kind]['state']=='active' and final[kind]['bytes']==start[kind]['bytes'] and final[kind]['receipt']=={}
    assert final['mutable']==first['mutable'] and final['malformed']==first['malformed']
    assert final['wrong_digest']['updated_at']>first['wrong_digest']['updated_at']
    proven = final['materialized']
    assert proven['state']=='settled' and proven['bytes']==0
    assert proven['receipt']['status']=='verified_immutable' and proven['receipt']['etag']==original_etag
    assert proven['receipt']['sha256']==seeded['sha256']
    current = store.raw_client.head_object(Bucket=store.bucket,Key=seeded['key'])
    assert current['VersionId']==physical['VersionId']
    result = {'started':start,'first_page':first,'after_restart':final,'same_owner_write_while_proof_waited':True,
              'foreign_account_refused':True,'s3_version_unchanged_after_reconciliation':True,'payload_bytes':seeded['size']}
    (api.root/'case-reservation-rotation.json').write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    from live_llm_job_repeat_acceptance import main

    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=8))
