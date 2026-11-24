"""Versioned SeaweedFS, real owner admission and DeepSeek evidence readback."""
import asyncio
import json


FIXTURE = '''
import asyncio
import hashlib
import os
import uuid
from datetime import datetime,timedelta,timezone
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import OwnerStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import owner_storage_budget as budget
from app.services.durable_events import json_text
from app.services.storage_admission import lock_owner

budget.RECONCILE_PAGE_SIZE = 2
(root/'reservation-api-pid').write_text(str(os.getpid()))

def remote_records(tickets,prefix):
    raw = budget.raw_client()
    bucket = json.loads(tickets['active']['binding_json'])['bucket']
    content = b'verified old immutable data'
    path = prefix.rstrip('/')+'/.vibeflow/live-store/objects/'+str(uuid.uuid4())+'/capacity-proof.bin'
    raw.put_object(Bucket=bucket,Key=path,Body=content,IfNoneMatch='*')
    known = {'schema':'vibeflow.remote-write.v1','bucket':bucket,'key':path,'size':len(content),
             'sha256':hashlib.sha256(content).hexdigest(),'if_match':None,'if_none_match':'*'}
    mutable = {**known,'key':prefix.rstrip('/')+'/.vibeflow/live-store/head.json',
               'size':64,'if_match':'"unresolved-old-head"','if_none_match':None}
    pending = {'invalid':None,'mutable':mutable,'proven':known,'wrong_digest':{**known,'sha256':'f'*64},
               'active':None,'closed_missing_receipt':None}
    receipts = {}
    for kind,ticket in tickets.items():
        receipt,encoded = budget.document(ticket,closed=kind!='active',pending=pending[kind],used=128)
        raw.put_object(**budget.key(ticket,'head.json'),Body=b'[]' if kind=='invalid' else encoded,IfNoneMatch='*')
        if kind != 'invalid': receipts[kind]=receipt
    return receipts

@app.post('/api/v1/cases/{case_id}/testing/reservation-history')
async def seed_history(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    assert (await budget.owner_for_case(db,case)).id == user.id
    await lock_owner(db,user.id)
    kinds = ('invalid','mutable','proven','wrong_digest','active','closed_missing_receipt')
    origin = datetime(2020,1,1,tzinfo=timezone.utc)
    writer = json_text(await asyncio.to_thread(budget.identity.observe,os.getpid()))
    rows = {}
    for offset,kind in enumerate(kinds):
        at = origin + timedelta(seconds=offset)
        rows[kind] = OwnerStorageReservation(id=str(uuid.uuid4()),user_id=user.id,
            binding_json=json_text(budget.binding(user.id)),writer_json=writer, size=65536,
            remaining_bytes=65536,state='active',receipt_json='{}',created_at=at,updated_at=at)
    receipts = await asyncio.to_thread(remote_records,{kind:budget.ticket_for(row) for kind,row in rows.items()},case.s3_prefix)
    rows['invalid'].receipt_json=json_text({'legacy_receipt':True})
    for kind in ('mutable','proven','wrong_digest'):
        rows[kind].receipt_json=json_text(receipts[kind])
        rows[kind].remaining_bytes=receipts[kind]['pending']['size']
    db.add_all(list(rows.values()))
    await db.commit()
    return {kind:row.id for kind,row in rows.items()}

@app.get('/api/v1/cases/{case_id}/testing/reservation-history')
async def list_history(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db,write=False)
    assert (await budget.owner_for_case(db,case)).id == user.id
    rows = await db.scalars(select(OwnerStorageReservation).where(
        OwnerStorageReservation.user_id==user.id, OwnerStorageReservation.created_at<datetime(2021,1,1,tzinfo=timezone.utc)))
    return {row.id:{'state':row.state,'bytes':row.remaining_bytes,'receipt':json.loads(row.receipt_json),
                    'updated_at':row.updated_at.isoformat()} for row in rows}
'''


def scenario(api, *, store=None, **_):
    from live_llm_failure_fallback_acceptance import kill_owned_api
    from live_llm_job_repeat_acceptance import put
    from live_llm_outbox_restart_acceptance import restart

    assert store is not None
    store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    assert store.raw_client.get_bucket_versioning(Bucket=store.bucket)['Status']=='Enabled'
    endpoint = api.prefix+'/testing/reservation-history'
    maintenance = '/case-provisions:maintenance'
    ids = api.call('POST',endpoint)
    owner = api.call('GET','/auth/me')['id']

    def remote_grant(kind):
        key = f'workspaces/{owner}/.storage-capacity/{ids[kind]}/head.json'
        response = store.raw_client.get_object(Bucket=store.bucket,Key=key)
        body = response['Body']
        try:
            return body.read(),response['ETag'],response['VersionId']
        finally:
            body.close()

    def snapshot():
        rows = api.call('GET',endpoint)
        return {kind:rows[identifier] for kind,identifier in ids.items()}

    start = snapshot()
    def denied():
        reply = api.call('POST',maintenance,expect=409)
        assert reply['detail']['code']=='storage_quota_busy',reply

    denied()
    first = snapshot()
    assert first['invalid']['updated_at'] > start['invalid']['updated_at']
    assert first['invalid']['receipt'] == start['invalid']['receipt']
    assert first['invalid']['bytes'] == start['invalid']['bytes'] == 65536
    assert first['mutable']['bytes'] == start['mutable']['bytes'] == 64
    assert first['proven'] == start['proven'] and first['wrong_digest'] == start['wrong_digest']
    assert first['active']['receipt'] == {} and first['active']['bytes'] == start['active']['bytes']
    assert first['closed_missing_receipt']['state']=='settled' and first['closed_missing_receipt']['bytes']==0

    denied()
    second = snapshot()
    assert second['invalid']==first['invalid'] and second['mutable']==first['mutable']
    assert second['proven']['state']=='settled' and second['proven']['bytes']==0
    assert second['proven']['receipt']['immutable_receipt']['status']=='verified_immutable'
    assert second['wrong_digest']['updated_at']>first['wrong_digest']['updated_at']
    assert second['wrong_digest']['state']=='active' and second['wrong_digest']['bytes']==start['wrong_digest']['bytes']
    assert second['active']['receipt'] == {} and second['active']['bytes']==start['active']['bytes']
    open_record, open_etag, open_version = remote_grant('active')
    assert not json.loads(open_record)['closed']
    malformed = remote_grant('invalid')
    assert malformed[0] == b'[]'

    # Real process exit is the evidence for fencing the last open grant.
    kill_owned_api(api,int((api.root/'reservation-api-pid').read_text()))
    restart(api)
    api.call('POST',maintenance)
    final = snapshot()
    assert final['active']['state']=='settled' and final['active']['bytes']==0 and final['active']['receipt']['closed']
    closed_record, closed_etag, closed_version = remote_grant('active')
    assert json.loads(closed_record)['closed'] and closed_etag!=open_etag and closed_version!=open_version
    assert remote_grant('invalid')==malformed
    assert final['invalid']['receipt']==start['invalid']['receipt']
    for kind in ('invalid','mutable','wrong_digest'):
        assert final[kind]['state']=='active' and final[kind]['bytes']==start[kind]['bytes']
    put(api,'Ref/after-capacity-recovery.txt','New writes after proven recovery')
    assert api.call('GET',api.prefix+'/file?path=Ref/after-capacity-recovery.txt')['content']=='New writes after proven recovery'
    result = {'blocked_admissions':2,'initial':start,'first_pass':first,'second_pass':second,'after_process_exit':final,
              'closed_grant_settlement_survives_denial':True,'new_write_after_proven_exit':True,
              'actual_remote_grant_version_advanced':True,'corrupt_remote_journal_version_unchanged':True}
    (api.root/'reservation-rotation.json').write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    from live_llm_job_repeat_acceptance import main

    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=8))
