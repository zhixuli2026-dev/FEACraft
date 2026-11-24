"""Compact actual S3 receipts on a restarted API and verify database rollback."""
import asyncio
import base64
import json
import subprocess
import sys

import httpx

from app.reservation_receipts import PREFIX, unpack
from live_llm_case_reservation_rotation_acceptance import FIXTURE as S3_FIXTURE
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart
from testsupport import SERVER


FIXTURE = S3_FIXTURE + '''
from sqlalchemy import Text, cast
from app.models import OwnerStorageReservation, UploadChunkCredit
from app.services import owner_storage_budget, reservation_history

get_settings().storage_maintenance_enabled = (root/'enable-receipt-compaction').is_file()
get_settings().storage_maintenance_interval_seconds = 1
reservation_history.PAGE_SIZE = 2
case_ticket_fields = ('id', 'operation_id', 'user_id', 'case_id', 'binding_json', 'request_json', 'writer_json', 'size')

@app.post('/api/v1/cases/{case_id}/testing/history/{identifier}:seed')
async def incomplete_history(case_id:int, identifier:str, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db)
    row = await db.get(StorageWriteReservation, identifier)
    assert row.case_id == case_id and row.user_id == user.id and row.state == 'settled'
    common = {key:getattr(row, key) for key in case_ticket_fields if key != 'id'}
    ids = [f'00000000-0000-0000-0000-{index:012x}' for index in range(1, 4)]
    for item in ids:
        db.add(StorageWriteReservation(id=item, **common, remaining_bytes=0, state='settled',
            receipt_json='{"schema":"vibeflow.remote-write-receipt.v1","status":200,"incomplete":true}'))
    await db.commit()
    return ids

@app.get('/api/v1/cases/{case_id}/testing/history/{upload_id}')
async def history(case_id:int, upload_id:str, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db, write=False)
    async def rows(model, predicate):
        stored = cast(model.receipt_json, Text).label('stored_receipt')
        statement = select(model, stored).where(model.user_id == user.id, predicate)
        return {row.id:{'raw':raw, 'logical':row.receipt_json, 'state':row.state, 'remaining':row.remaining_bytes,
            'size':row.size, 'binding':row.binding_json, 'created':row.created_at.isoformat(),
            'updated':row.updated_at.isoformat()} for row, raw in await db.execute(statement)}
    credits = (await db.scalars(select(UploadChunkCredit).where(UploadChunkCredit.upload_id == upload_id))).all()
    ids = [row.id for row in credits]
    return {'owner':await rows(OwnerStorageReservation, OwnerStorageReservation.id.in_(ids)),
        'case':await rows(StorageWriteReservation, StorageWriteReservation.case_id == case_id),
        'credits':{row.id:[row.offset, row.amount, row.sha256, row.state, row.receipt_json] for row in credits}}

@app.post('/api/v1/cases/{case_id}/testing/history/{identifier}:settle')
async def replay(case_id:int, identifier:str, payload:dict, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db)
    if payload['ledger'] == 'owner':
        row = await db.get(OwnerStorageReservation, identifier)
        assert row.user_id == user.id and row.state == 'settled'
        await owner_storage_budget.settle(db, owner_storage_budget.ticket_for(row), {})
        await db.commit()
    else:
        row = await db.get(StorageWriteReservation, identifier)
        assert row.case_id == case_id and row.user_id == user.id and row.state == 'settled'
        ticket = {key:getattr(row, key) for key in case_ticket_fields}
        await db.rollback()
        await budget.settle(ticket, {})
    return {'settled':True}

@app.post('/api/v1/cases/{case_id}/testing/history:stop')
async def stop_schedule(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db)
    await db.rollback()
    app.state.storage_maintenance_stop.set()
    await asyncio.wait_for(app.state.storage_maintenance_task, timeout=30)
    return {'stopped':True}
'''


def scenario(api, *, store, **_):
    case_proof = api.prefix + '/testing/case-reservations'
    seeded = api.call('POST', case_proof)
    for _ in range(2):
        assert api.call('POST', case_proof + ':review')['ok']
    case_id = seeded['rows']['materialized']
    proof = api.call('GET', case_proof)[case_id]['receipt']
    object_tag = store.raw_client.head_object(Bucket=store.bucket, Key=seeded['key'])['ETag']
    assert proof['etag'] == object_tag and proof['sha256'] == seeded['sha256']
    assert proof['status'] == 'verified_immutable'

    route = api.prefix + '/testing/history'
    incomplete = api.call('POST', f'{route}/{case_id}:seed')
    uploads = api.prefix + '/uploads'
    upload = api.call('POST', uploads, {'path':'Ref/scheduled-receipt-proof.bin', 'size':9000})
    upload_id = upload['id']
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                      timeout=30, trust_env=False) as client:
        response = client.put(f'{uploads}/{upload_id}?offset=0', content=b'persisted S3 upload chunk')
        response.raise_for_status()
        offset = response.json()['offset']
    history_url = f'{route}/{upload_id}'
    before = api.call('GET', history_url)
    owner_id, = before['credits']
    assert offset == len(b'persisted S3 upload chunk') and before['credits'][owner_id][3] == 'settled'
    assert before['owner'][owner_id]['state'] == 'settled'
    assert not before['case'][case_id]['raw'].startswith(PREFIX)
    (api.root/'enable-receipt-compaction').touch()
    kill_owned_api(api, int((api.root/'case-reservation-pid').read_text()))
    restart(api)

    targets = {'owner':owner_id, 'case':case_id}

    def compressed():
        current = api.call('GET', history_url)
        if all(current[name][identifier]['raw'].startswith(PREFIX) for name, identifier in targets.items()):
            return current

    after = wait_for(compressed, 'Scheduled maintenance did not compact both S3 receipts', 45)
    assert api.call('POST', route + ':stop')['stopped']
    assert after['credits'] == before['credits']
    savings = {}
    for name, identifier in targets.items():
        original, changed = before[name][identifier], after[name][identifier]
        raw = changed['raw']
        assert {**changed, 'raw':original['raw']} == original and original['raw'] == original['logical']
        plain_size, packed_size = len(original['raw'].encode()), len(raw.encode())
        assert packed_size < plain_size
        savings[name] = {'original_bytes':plain_size, 'compressed_bytes':packed_size}
        version, size, encoded = json.loads(raw)
        damaged = bytearray(base64.b85decode(encoded))
        damaged[0] ^= 1
        try:
            unpack(json.dumps([version, size, base64.b85encode(damaged).decode()], separators=(',', ':')))
        except ValueError:
            pass
        else:
            raise AssertionError('Corrupt receipt was accepted')
        assert api.call('POST', f'{route}/{identifier}:settle', {'ledger':name})['settled']
    assert after['case'][case_id] == api.call('GET', history_url)['case'][case_id]
    active = [identifier for identifier in seeded['rows'].values() if identifier != case_id]
    for identifier in incomplete + active:
        assert after['case'][identifier] == before['case'][identifier]
    assert all(after['case'][identifier]['remaining'] > 0 for identifier in active)
    assert api.call('GET', f'{uploads}/{upload_id}')['offset'] == offset
    assert store.raw_client.head_object(Bucket=store.bucket, Key=seeded['key'])['ETag'] == object_tag

    def restore(*args):
        result = subprocess.run([sys.executable, '-B', 'reservation_history_restore.py', *args], cwd=SERVER,
                                env=api.env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, 'Receipt history restore CLI failed'
        return json.loads(result.stdout)

    pending = restore()['compressed']
    assert pending['owner_storage_reservations'] >= 1 and pending['storage_write_reservations'] >= 1
    assert api.call('GET', history_url)['owner'] == after['owner']
    restoration = restore('--apply')
    assert restoration['restored'] == pending and not any(restoration['still_compressed'].values())
    final = api.call('GET', history_url)
    for name, identifier in targets.items():
        assert final[name][identifier] == before[name][identifier]
    assert final['credits'] == before['credits']
    report = {'bytes':savings, 'restore':restoration, 'page_size':2,
              'active_and_incomplete_preserved':len(incomplete + active), 'upload_offset':offset,
              'duplicate_settlement_preserved_receipts':True, 'corrupt_digest_refused':True,
              'identifiers_binding_timestamps_and_source_etag_unchanged':True}
    (api.root/'reservation-history.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE, store_volume_max=8))
