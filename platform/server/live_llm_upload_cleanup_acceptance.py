"""Restart the actual upload API during staging cleanup and verify the published bytes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import http.client
import json
import sys
import time
import urllib.error
from uuid import uuid4

import httpx

from live_llm_job_repeat_acceptance import main, write
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_outbox_restart_acceptance import restart


FIXTURE = '''
import os
import time
from fastapi import Depends
from app.db import get_db, SessionLocal
from app.deps import get_current_user
from app.models import CaseEventCounter, StorageWriteReservation, UploadChunkCredit, UploadStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import uploads
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from app.models import OwnerStorageReservation
from app.services import owner_storage_budget, quota
from botocore.client import BaseClient

actual_quotas = quota.quotas_for
async def constrained_quotas(db, user):
    configured = await actual_quotas(db, user)
    limit = root/'chunk-storage-limit-mib'
    if limit.is_file():
        return {**configured, 'storage_mb':int(limit.read_text())}
    return configured
quota.quotas_for = constrained_quotas

@app.get('/api/v1/cases/{case_id}/testing/upload-cancel-sql-lock')
async def lock_probe(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id, user, db)
    async with SessionLocal() as probe:
        if probe.get_bind().dialect.name == 'postgresql':
            statement = select(CaseEventCounter.case_id).where(CaseEventCounter.case_id == case_id)
            try:
                assert await probe.scalar(statement.with_for_update(nowait=True)) == case_id
            except OperationalError as error:
                if getattr(error.orig, 'sqlstate', None) != '55P03':
                    raise
                return {'acquired':False}
        else:
            connection = await probe.connection()
            previous = (await connection.exec_driver_sql('PRAGMA busy_timeout')).scalar_one()
            try:
                await connection.exec_driver_sql('PRAGMA busy_timeout=200')
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
            except OperationalError as error:
                if getattr(error.orig, 'sqlite_errorcode', None) != 5:
                    raise
                return {'acquired':False}
            finally:
                await connection.exec_driver_sql('PRAGMA busy_timeout='+str(previous))
        await probe.rollback()
    return {'acquired':True}

original_request = BaseClient._make_api_call
def interrupt_s3(client, operation, arguments):
    pause = root/'pause-cancel-page'
    release = root/'release-cancel-page'
    if (operation == 'ListObjectsV2' and arguments.get('ContinuationToken') and pause.is_file()
            and arguments.get('Prefix') == pause.read_text() and not release.exists()):
        (root/'cancel-page-paused').touch()
        expires = time.monotonic() + 35
        while not release.exists():
            if time.monotonic() >= expires:
                raise TimeoutError('Test did not release second S3 cancellation page')
            time.sleep(.025)
    chunk = root/'pause-chunk-put'
    release_chunk = root/'release-chunk-put'
    if (operation == 'PutObject' and chunk.is_file() and arguments.get('Key') == chunk.read_text()
            and not release_chunk.exists()):
        from app.services.request_deadline import current
        deadline = current()
        if deadline is not None:
            (root/'chunk-put-deadline-remaining').write_text(str(deadline.remaining()))
        (root/'chunk-put-paused').touch()
        expires = time.monotonic() + 35
        while not release_chunk.exists():
            if time.monotonic() >= expires:
                raise TimeoutError('Test did not release the actual chunk PUT')
            time.sleep(.025)
    trigger = root / 'exit-at-object'
    cut = json.loads(trigger.read_text()) if operation == 'PutObject' and trigger.exists() else None
    selected = cut and arguments.get('Key', '').endswith(cut['key'])
    if selected and cut['stage'] == 'before':
        os._exit(88)
    response = original_request(client, operation, arguments)
    if selected:
        os._exit(89)
    return response
BaseClient._make_api_call = interrupt_s3

@app.post('/api/v1/testing/upload-pools')
async def reconcile_pools(user=Depends(get_current_user), db=Depends(get_db)):
    await owner_storage_budget.recover_owner(user.id, wait=True)
    rows = await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id == user.id))
    return [{'remaining':r.remaining_bytes, 'state':r.state,
             'pending':(json.loads(r.receipt_json).get('pending') or {}).get('key'),
             'proof':(json.loads(r.receipt_json).get('upload_chunk_receipt') or {}).get('schema')} for r in rows]

original_unlink = Path.unlink
def interrupt_cleanup(path, *args, **kwargs):
    original_unlink(path, *args, **kwargs)
    trigger = root / 'exit-after-unlink'
    if trigger.is_file() and path.parent.name + '/' + path.name == trigger.read_text():
        os._exit(87)
Path.unlink = interrupt_cleanup

@app.get('/api/v1/cases/{case_id}/testing/upload/{identifier}')
async def ledger(case_id:int, identifier:str, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    row = await db.get(UploadStorageReservation, identifier)
    assert row and row.case_id == case.id
    receipt = await uploads.completion_record(db, case, identifier)
    return {'state':row.state, 'remaining':row.remaining_bytes, 'receipt':receipt}

@app.get('/api/v1/cases/{case_id}/testing/chunk-pool/{identifier}')
async def chunk_pool(case_id:int, identifier:str, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    row = await db.get(UploadStorageReservation, identifier)
    assert row and row.case_id == case.id
    statement = select(UploadChunkCredit, OwnerStorageReservation).join(
        OwnerStorageReservation, OwnerStorageReservation.id == UploadChunkCredit.id
    ).where(UploadChunkCredit.upload_id == identifier).order_by(UploadChunkCredit.created_at.desc()).limit(1)
    credit, pool = (await db.execute(statement)).one()
    return {'transferred':credit.amount,'credit_state':credit.state,'pool_size':pool.size,
            'pool_remaining':pool.remaining_bytes,'upload_remaining':row.remaining_bytes}

@app.get('/api/v1/cases/{case_id}/testing/upload-close-credits/{identifier}')
async def close_credits(case_id:int, identifier:str, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    stem = f'{case.s3_prefix}/.vibeflow/uploads/{identifier}/chunks/'
    rows = await db.scalars(select(StorageWriteReservation).where(
        StorageWriteReservation.case_id == case.id, StorageWriteReservation.size == 0))
    return [{'state':row.state, 'remaining':row.remaining_bytes, 'proof':json.loads(row.receipt_json).get('status')}
            for row in rows if json.loads(row.request_json)['key'].startswith(stem)]

@app.post('/api/v1/cases/{case_id}/testing/legacy-upload')
async def legacy(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    return uploads.create_session(case.s3_prefix, 'Ref/legacy.bin', 4321, None, False)

@app.post('/api/v1/cases/{case_id}/testing/early-settlement/{identifier}')
async def old_settlement(case_id:int, identifier:str, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    row = await db.get(UploadStorageReservation, identifier)
    assert row.case_id == case.id and row.state == 'active'
    row.state = 'cancelled'
    row.remaining_bytes = 0
    await db.commit()
    return {'ok':True}
'''


def local_scenario(api):
    data = b'actual upload cleanup recovery\n' * 500
    digest = hashlib.sha256(data).hexdigest()
    uploads = api.prefix + '/uploads'
    trigger = api.root / 'exit-after-unlink'
    api.put('Ref/neighbour.txt', 'keep')
    checks = []

    def ledger(identifier):
        return api.call('GET', api.prefix + '/testing/upload/' + identifier)

    def append(identifier, payload):
        response = httpx.put(api.base + '/api/v1' + uploads + '/' + identifier + '?offset=0',
                             headers={'Authorization':'Bearer ' + api.token}, content=payload,
                             trust_env=False, timeout=30)
        response.raise_for_status()
        assert response.json()['offset'] == len(payload)

    def crash(method, route, cut):
        trigger.write_text(cut)
        try:
            api.call(method, route)
        except (http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
            pass
        assert api.proc.wait(timeout=10) == 87
        trigger.unlink()
        restart(api)

    for action, cut, legacy in [('cancel', 'data.part', False), ('cancel', 'meta.json', True),
                                ('complete', 'data.part', False), ('complete', 'meta.json', False)]:
        path = 'Ref/' + action + '-' + cut + '.bin'
        if legacy:
            item = api.call('POST', api.prefix + '/testing/legacy-upload')
            path = item['path']
        else:
            item = api.call('POST', uploads, {'path':path, 'size':len(data), 'sha256':digest})
            append(item['id'], data if action == 'complete' else data[:30])
        identifier = item['id']
        endpoint = uploads + '/' + identifier
        staging, = (api.root / 'files').rglob(identifier)
        method, route, phase = (('DELETE', endpoint, 'cancelling') if action == 'cancel'
                                else ('POST', endpoint + ':complete', 'completing'))
        retired = staging.with_name(identifier + '.' + phase)
        crash(method, route, retired.name + '/' + cut)
        assert not staging.exists() and retired.is_dir()
        assert {p.name for p in retired.iterdir()} == ({'meta.json'} if cut == 'data.part' else set())
        before = ledger(identifier)
        if action == 'cancel':
            assert before['state'] == 'active' and before['remaining'] == item['size'] - (0 if legacy else 30)
        else:
            assert before['state'] == 'completed' and before['remaining'] == 0
            assert before['receipt']['result']['sha256'] == digest
        if cut == 'data.part':
            unknown = retired / 'unrelated.bin'
            unknown.write_bytes(b'keep')
            if action == 'cancel':
                api.call(method, route, expect=409)
            else:
                assert api.call(method, route)['cleanup_pending']
            assert unknown.read_bytes() == b'keep' and ledger(identifier) == before
            unknown.unlink()
        response = api.call(method, route)
        assert api.call(method, route) == response and not retired.exists()
        settled = ledger(identifier)
        assert settled['remaining'] == 0
        if action == 'complete':
            assert settled == before and 'cleanup_pending' not in response
            assert api.call('GET', api.prefix + '/raw?path=' + path, raw=True) == data
            api.call('DELETE', endpoint, expect=409)
        else:
            assert settled == {'state':'cancelled', 'remaining':0, 'receipt':None}
            api.call('POST', endpoint + ':complete', expect=409)
            api.call('GET', api.prefix + '/raw?path=' + path, expect=404, raw=True)
        assert api.call('GET', api.prefix + '/raw?path=Ref/neighbour.txt', raw=True) == b'keep'
        checks.append({'action':action, 'killed_after_unlink':cut, 'legacy':legacy,
                       'remaining_before_retry':before['remaining'], 'remaining_after_retry':settled['remaining'],
                       'duplicate_response_equal':True, 'unknown_file_retained':cut == 'data.part'})
    report = {'passed':True, 'payload_sha256':digest, 'api_process_deaths':len(checks), 'checks':checks}
    (api.root / 'upload-cleanup.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


def remote_scenario(api, store):
    from botocore.exceptions import ClientError
    base = api.prefix + '/uploads'
    path = 'Ref/cancelled-upload.bin'
    item = api.call('POST', base, {'path':path, 'size':9000})
    identifier = item['id']
    endpoint = base + '/' + identifier
    keys = [v['Key'] for page in store.raw_client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
            for v in page.get('Contents', []) if v['Key'].endswith('/' + identifier + '/meta.json')]
    meta_key, = keys
    prefix = meta_key.removesuffix('/meta.json')
    trigger = api.root / 'exit-at-object'
    raw = store.raw_client

    def read(key):
        value = raw.get_object(Bucket=store.bucket, Key=key)
        try:
            return value['Body'].read(), value['ETag']
        finally:
            value['Body'].close()

    def obsolete_put(key, data, **condition):
        try:
            raw.put_object(Bucket=store.bucket, Key=key, Body=data, **condition)
        except ClientError as error:
            assert error.response['ResponseMetadata']['HTTPStatusCode'] == 412
        else:
            raise AssertionError('Late object request replaced a closed upload')

    def cut(call, key, stage, code):
        trigger.write_text(json.dumps({'key':key, 'stage':stage}))
        try:
            call()
        except (httpx.TransportError, http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
            pass
        assert api.proc.wait(timeout=10) == code
        trigger.unlink()
        restart(api)

    api.put('Ref/neighbour.txt', 'keep')
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                      timeout=60, trust_env=False) as client:
        client.put(endpoint + '?offset=0', content=b'A' * 100).raise_for_status()
        late = prefix + '/chunks/0000000100/'
        interrupted_key = 'record.json' if api.database_url else 'data'
        cut(lambda: client.put(endpoint + '?offset=100', content=b'B' * 50).raise_for_status(),
            late + interrupted_key, 'before', 88)
    before, old_etag = read(meta_key)
    assert json.loads(before)['offset'] == 100
    cut(lambda: api.call('DELETE', endpoint), meta_key, 'after', 89)
    terminal, etag = read(meta_key)
    assert etag != old_etag and json.loads(terminal)['status'] == 'cancelled'
    state_url = api.prefix + '/testing/upload/' + identifier
    assert api.call('GET', state_url)['state'] == 'active'
    # Identical JSON semantics with different bytes cannot prove the pending request.
    changed = raw.put_object(Bucket=store.bucket, Key=meta_key, Body=terminal+b' ', IfMatch=etag)
    pools = api.call('POST', '/testing/upload-pools')
    pending, = [r for r in pools if r['pending'] == meta_key]
    assert pending['state'] == 'active' and pending['remaining'] == len(terminal)
    raw.put_object(Bucket=store.bucket, Key=meta_key, Body=terminal, IfMatch=changed['ETag'])
    pools = api.call('POST', '/testing/upload-pools')
    proof, = [r for r in pools if r['proof'] == 'vibeflow.upload-cancel-proof.v1']
    assert proof['state'] == 'settled' and proof['remaining'] == 0
    assert api.call('GET', state_url)['state'] == 'active'
    assert api.call('DELETE', endpoint) == api.call('DELETE', endpoint) == {'ok':True, 'cancelled':True}
    assert api.call('GET', state_url) == {'receipt':None, 'state':'cancelled', 'remaining':0}
    assert all(r['state']=='settled' and r['remaining']==0 for r in api.call('POST', '/testing/upload-pools'))
    for offset in (0, 100):
        for name in ('data', 'record.json'):
            key = prefix + f'/chunks/{offset:010d}/' + name
            assert read(key)[0] == b''
            obsolete_put(key, b'late', IfNoneMatch='*')
    obsolete_put(meta_key, before, IfMatch=old_etag)
    assert read(meta_key)[0] == terminal
    # A row settled by an older server can coexist with materialized chunk files.
    legacy = api.call('POST', base, {'path':'Ref/legacy-s3.bin', 'size':200})['id']
    route = base + '/' + legacy
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                      timeout=60, trust_env=False) as client:
        client.put(route + '?offset=0', content=b'L' * 30).raise_for_status()
    legacy_meta = meta_key.replace('/'+identifier+'/', '/'+legacy+'/')
    existing, version = read(legacy_meta)
    api.call('POST', api.prefix + '/testing/early-settlement/' + legacy)
    api.call('DELETE', route, expect=409)
    assert read(legacy_meta) == (existing, version)
    document = json.loads(existing)
    document['status'] = 'cancelled'
    raw.put_object(Bucket=store.bucket, Key=legacy_meta, Body=json.dumps(document).encode(), IfMatch=version)
    assert api.call('DELETE', route)['cancelled']
    for name in ('data', 'record.json'):
        assert read(legacy_meta.removesuffix('meta.json')+'chunks/0000000000/'+name)[0] == b''
    assert api.call('GET', api.prefix + '/raw?path=Ref/neighbour.txt', raw=True) == b'keep'
    api.call('GET', api.prefix + '/raw?path=' + path, expect=404, raw=True)

    # The keyed delete reenters the existing lease after a real marker PUT.
    key = {'Idempotency-Key':'cancel-keyed-'+uuid4().hex}
    keyed = api.call('POST', base, {'path':'Ref/keyed-cancel.bin', 'size':200}, extra_headers=key)['id']
    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=60, trust_env=False) as client:
        client.put(base+'/'+keyed+'?offset=0', content=b'keyed content').raise_for_status()
    keyed_prefix = prefix.replace('/'+identifier, '/'+keyed)
    marker = keyed_prefix+'/chunks/0000000000/data'
    lagging = keyed_prefix+'/chunks/0000000000/record.json'
    intent = api.prefix+'/upload-intent'
    cut(lambda:api.call('DELETE', intent, extra_headers=key), marker, 'after', 89)
    assert read(marker)[0] == b'' and read(lagging)[0]
    credits_url = api.prefix+'/testing/upload-close-credits/'+keyed
    assert api.call('GET', credits_url) == [{'state':'active','remaining':0,'proof':None}]
    assert api.call('DELETE', intent, extra_headers=key)['cancelled']
    assert api.call('GET', intent, extra_headers=key)['cancelled']
    settled_credits = api.call('GET', credits_url)
    assert all(row['state']=='settled' and row['remaining']==0 for row in settled_credits)
    assert any(row['proof']=='verified_closed_upload_chunk' for row in settled_credits)
    assert read(lagging)[0] == b''
    obsolete_put(marker, b'late', IfNoneMatch='*')

    manual = api.call('POST', base, {'path':'Ref/manual-cancel.bin', 'size':400})['id']
    recovery = api.prefix+'/upload-recovery'
    plan = api.call('GET', recovery+'/'+manual)
    confirmation = {'action':'cancel_upload','upload_id':manual,
                    'snapshot_sha256':plan['snapshot_sha256'],'note':'Checked actual cancellation'}
    action = lambda:api.call('POST', recovery+'/actions', confirmation, expect=202,
                             extra_headers={'Idempotency-Key':'cancel-reviewed-'+manual})
    manual_meta = meta_key.replace('/'+identifier+'/', '/'+manual+'/')
    cut(action, manual_meta, 'after', 89)
    assert api.call('GET', api.prefix+'/testing/upload/'+manual)['state']=='active'
    accepted = action()
    assert accepted['state']=='completed' and accepted['result']['cancelled'] and action()==accepted
    assert api.call('GET', api.prefix+'/testing/upload/'+manual)['remaining']==0

    retry_payload = bytes(range(256)) * 4096
    retry_id = api.call('POST', base, {'path':'Ref/retry-cold-body.bin','size':len(retry_payload)})['id']
    retry_route = base+'/'+retry_id
    retry_meta = meta_key.replace('/'+identifier+'/', '/'+retry_id+'/')
    retry_body = retry_meta.removesuffix('meta.json')+'chunks/0000000000/data'
    retry_head = api.prefix+'/testing/chunk-pool/'+retry_id
    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=60, trust_env=False) as client:
        cut(lambda:client.put(retry_route+'?offset=0', content=retry_payload), retry_body, 'after', 89)
        actual, version = read(retry_body)
        assert actual == retry_payload and json.loads(read(retry_meta)[0])['offset'] == 0
        assert api.call('GET', retry_head)['credit_state']=='active'
        incorrect = client.put(retry_route+'?offset=0', content=b'Q'*len(retry_payload))
        assert incorrect.status_code == 409, incorrect.text
        assert read(retry_body) == (retry_payload, version)
        replay = client.put(retry_route+'?offset=0', content=retry_payload)
        replay.raise_for_status()
        assert replay.json()['offset'] == len(retry_payload)
    assert read(retry_body) == (retry_payload, version)
    repeated = api.call('GET', retry_head)
    assert repeated['transferred']==0 and repeated['credit_state']=='settled' and repeated['pool_remaining']==0
    assert api.call('POST', retry_route+':complete')['sha256']==hashlib.sha256(retry_payload).hexdigest()
    assert api.call('GET', api.prefix+'/raw?path=Ref/retry-cold-body.bin', raw=True)==retry_payload

    report = {'passed':True, 'api_process_deaths':5, 'interrupted_chunk_offset':100,
              'interrupted_chunk_key':interrupted_key,
              'different_bytes_retained':len(terminal), 'identical_terminal_bytes_reconciled':True,
              'late_requests_rejected_412':6, 'all_pools_settled':True,
              'keyed_cancel_recovered_marker':True, 'reviewed_cancel_reused_receipt':True,
              'cold_chunk_reused_original_etag_without_second_transfer':True,
              'legacy_settlement_recovered':True, 'replacement_upload_preserved':True}
    (api.root / 'upload-cleanup.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


def scenario(api, *, store=None, **_):
    return remote_scenario(api, store) if store else local_scenario(api)


def cancellation_lock_scenario(api, *, store, baseline=False, **_):
    assert store is not None
    workspace = api.call('GET','/workspaces')[0]['id']
    sibling = write(api, 'POST', f'/workspaces/{workspace}/cases', {'name':'Sibling during cancellation'})['id']
    upload = api.call('POST', api.prefix+'/uploads', {'path':'Ref/cancel-lock.bin', 'size':1024})
    identifier = upload['id']
    endpoint = api.prefix+'/uploads/'+identifier
    with httpx.Client(trust_env=False, timeout=30) as client:
        client.put(api.base+'/api/v1'+endpoint+'?offset=0',
                   headers={'Authorization':'Bearer '+api.token}, content=b'actual first chunk').raise_for_status()
    raw = store.raw_client
    meta, = [item['Key'] for page in raw.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
             for item in page.get('Contents', []) if item['Key'].endswith('/'+identifier+'/meta.json')]
    stem = meta.removesuffix('meta.json')+'chunks/'
    for offset in range(100, 1800, 100):
        for name in ('data', 'record.json'):
            raw.put_object(Bucket=store.bucket, Key=f'{stem}{offset:010d}/{name}', Body=b'legacy staged content')
    (api.root/'pause-cancel-page').write_text(stem)
    sibling_path = f'/cases/{sibling}/file'
    marker = {'path':'Ref/save-during-cancel.txt', 'content':'actual sibling write'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(api.call, 'DELETE', endpoint)
        try:
            wait_for((api.root/'cancel-page-paused').exists, 'Cancellation did not reach second actual S3 page', 65)
            page = raw.list_objects_v2(Bucket=store.bucket, Prefix=stem)['Contents']
            closed = sum(item['Size']==0 for item in page)
            assert closed == 32
            sql_free = api.call('GET', api.prefix+'/testing/upload-cancel-sql-lock')['acquired']
            writing = pool.submit(write, api, 'PUT', sibling_path, marker)
            try:
                write_during_pause = writing.result(timeout=7 if baseline else 20)['ok']
            except FutureTimeout:
                write_during_pause = False
            assert not cancelled.done()
        finally:
            (api.root/'release-cancel-page').touch()
        assert cancelled.result(timeout=90) == {'ok':True, 'cancelled':True}
        assert writing.result(timeout=35)['ok']
    assert sql_free is not baseline and write_during_pause is not baseline
    assert api.call('GET', f'/cases/{sibling}/raw?path=Ref/save-during-cancel.txt', raw=True) == b'actual sibling write'
    assert api.call('GET', api.prefix+'/testing/upload/'+identifier)['remaining'] == 0
    assert api.call('GET', api.prefix+'/raw?path=Ref/cancel-lock.bin', expect=404, raw=True)
    closed_after = raw.list_objects_v2(Bucket=store.bucket, Prefix=stem)['Contents']
    assert len(closed_after) == 36 and all(item['Size']==0 for item in closed_after)
    report = {'passed':True, 'baseline':baseline, 'real_objects':len(closed_after),
              'closed_before_second_page':closed, 'sql_lock_available_during_listing':sql_free,
              'same_owner_other_case_saved_during_listing':write_during_pause}
    (api.root/'upload-cancellation-lock.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def chunk_lock_scenario(api, *, store, baseline=False, **_):
    assert store is not None
    workspace = api.call('GET','/workspaces')[0]['id']
    sibling = write(api, 'POST', f'/workspaces/{workspace}/cases', {'name':'Sibling during upload'})['id']
    payload = bytes(range(256)) * 16384
    target = 'Ref/chunk-lock.bin'
    upload = api.call('POST', api.prefix+'/uploads', {'path':target, 'size':len(payload)})
    sibling_payload = b'actual parallel upload body' * 64
    sibling_upload = api.call('POST', f'/cases/{sibling}/uploads',
                              {'path':'Ref/second-chunk.bin', 'size':len(sibling_payload)})
    identifier = upload['id']
    endpoint = api.prefix+'/uploads/'+identifier
    meta, = [item['Key'] for page in store.raw_client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
             for item in page.get('Contents', []) if item['Key'].endswith('/'+identifier+'/meta.json')]
    chunk = meta.removesuffix('meta.json')+'chunks/0000000000/data'
    (api.root/'pause-chunk-put').write_text(chunk)

    def put_chunk(body):
        return httpx.put(api.base+'/api/v1'+endpoint+'?offset=0', content=body,
                         headers={'Authorization':'Bearer '+api.token}, trust_env=False, timeout=75)

    marker = {'path':'Ref/save-during-chunk.txt', 'content':'actual sibling write'}
    with ThreadPoolExecutor(max_workers=2) as workers:
        posted = workers.submit(put_chunk, payload)
        try:
            wait_for((api.root/'chunk-put-paused').exists, 'Actual upload body PUT did not pause', 45)
            sql_free = api.call('GET', api.prefix+'/testing/upload-cancel-sql-lock')['acquired']
            pool_before = api.call('GET', api.prefix+'/testing/chunk-pool/'+identifier)
            assert pool_before['transferred'] == len(payload) and pool_before['credit_state']=='active'
            assert pool_before['upload_remaining'] == 0
            sibling_write = workers.submit(write, api, 'PUT', f'/cases/{sibling}/file', marker)
            try:
                write_during_pause = sibling_write.result(timeout=7 if baseline else 20)['ok']
            except FutureTimeout:
                write_during_pause = False
            assert not posted.done()
            if not baseline:
                competing = put_chunk(payload)
                assert competing.status_code == 409, competing.text
                assert len(payload) <= pool_before['pool_size'] < len(payload)+192*1024
                sibling_chunk = httpx.put(api.base+f'/api/v1/cases/{sibling}/uploads/{sibling_upload["id"]}?offset=0',
                                          content=sibling_payload, headers={'Authorization':'Bearer '+api.token},
                                          trust_env=False, timeout=15)
                sibling_chunk.raise_for_status()
                assert sibling_chunk.json()['offset']==len(sibling_payload)
                assert not posted.done()
        finally:
            (api.root/'release-chunk-put').touch()
        result = posted.result(timeout=90)
        result.raise_for_status()
        assert result.json()['offset']==len(payload)
        assert sibling_write.result(timeout=35)['ok']
    assert sql_free is not baseline and write_during_pause is not baseline
    assert api.call('POST', endpoint+':complete')['sha256']==hashlib.sha256(payload).hexdigest()
    assert api.call('GET', api.prefix+'/raw?path='+target, raw=True)==payload
    assert api.call('GET', f'/cases/{sibling}/raw?path=Ref/save-during-chunk.txt', raw=True)==b'actual sibling write'
    pool_after = api.call('GET', api.prefix+'/testing/chunk-pool/'+identifier)
    assert pool_after['credit_state']=='settled' and pool_after['pool_remaining']==0
    quota_proof = None
    if not baseline:
        sibling_endpoint = f'/cases/{sibling}/uploads/{sibling_upload["id"]}'
        assert api.call('POST', sibling_endpoint+':complete')['sha256']==hashlib.sha256(sibling_payload).hexdigest()
        assert api.call('GET', f'/cases/{sibling}/raw?path=Ref/second-chunk.bin', raw=True)==sibling_payload
        quota_proof = chunk_quota_scenario(api, store)
    report = {'passed':True, 'baseline':baseline, 'bytes':len(payload), 'sql_lock_available_during_put':sql_free,
              'same_owner_other_case_saved_during_put':write_during_pause, 'pool_before':pool_before,
              'same_owner_other_upload_advanced_during_put':not baseline, 'near_quota':quota_proof,
              'pool_after':pool_after, 'competing_same_offset_rejected':not baseline, 'published_bytes_match':True}
    (api.root/'upload-chunk-write-lock.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def chunk_quota_scenario(api, store):
    def physical_bytes():
        return sum(item['Size'] for page in store.raw_client.get_paginator('list_objects_v2').paginate(
            Bucket=store.bucket, Prefix='workspaces/1/') for item in page.get('Contents', []))

    before = physical_bytes()
    headroom = 150000
    mib = 1024 * 1024
    limit_mib = (before + headroom) // mib + 1
    amount = limit_mib * mib - before - headroom
    payload = (bytes(range(256)) * (amount // 256 + 1))[:amount]
    limit = api.root/'chunk-storage-limit-mib'
    limit.write_text(str(limit_mib))
    target = 'Ref/near-quota-chunk.bin'
    try:
        upload = api.call('POST', api.prefix+'/uploads', {'path':target,'size':amount})
        endpoint = api.prefix+'/uploads/'+upload['id']
        result = httpx.put(api.base+'/api/v1'+endpoint+'?offset=0', content=payload,
                           headers={'Authorization':'Bearer '+api.token}, trust_env=False, timeout=45)
        result.raise_for_status()
        assert result.json()['offset'] == amount
        credit = api.call('GET', api.prefix+'/testing/chunk-pool/'+upload['id'])
        assert credit['transferred'] == amount and credit['pool_remaining'] == 0
        physical = physical_bytes()
        assert physical <= limit_mib*mib
    finally:
        limit.unlink()
    assert api.call('POST', endpoint+':complete')['sha256']==hashlib.sha256(payload).hexdigest()
    assert api.call('GET', api.prefix+'/raw?path='+target, raw=True)==payload
    return {'declared_headroom':headroom, 'limit_bytes':limit_mib*mib, 'physical_bytes':physical,
            'transferred':amount, 'settled':True}


def chunk_timeout_scenario(api, *, store, **_):
    assert store is not None
    payload = bytes(range(256)) * 4096
    upload = api.call('POST', api.prefix+'/uploads', {'path':'Ref/chunk-timeout.bin','size':len(payload)})
    identifier = upload['id']
    endpoint = api.prefix+'/uploads/'+identifier
    raw = store.raw_client
    meta, = [item['Key'] for page in raw.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
             for item in page.get('Contents', []) if item['Key'].endswith('/'+identifier+'/meta.json')]
    stem = meta.removesuffix('meta.json')+'chunks/0000000000/'
    (api.root/'pause-chunk-put').write_text(stem+'data')

    def send():
        return httpx.put(api.base+'/api/v1'+endpoint+'?offset=0', content=payload,
                         headers={'Authorization':'Bearer '+api.token}, trust_env=False, timeout=65)

    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(send)
        try:
            wait_for((api.root/'chunk-put-paused').exists, 'Timed chunk did not start the object request', 40)
            remaining = float((api.root/'chunk-put-deadline-remaining').read_text())
            assert 0 < remaining < 20
            time.sleep(remaining + .25)
            assert not pending.done(), 'The expired request must still drain its started write'
        finally:
            (api.root/'release-chunk-put').touch()
        overloaded = pending.result(timeout=50)
    assert overloaded.status_code == 504 and overloaded.json()['detail']['code'] == 'upload_chunk_timeout'
    keys = raw.list_objects_v2(Bucket=store.bucket, Prefix=stem)['Contents']
    assert [item['Key'] for item in keys]==[stem+'data'] and keys[0]['Size']==len(payload)
    etag = keys[0]['ETag']
    assert api.call('GET', endpoint)['offset']==0
    pool = api.call('GET', api.prefix+'/testing/chunk-pool/'+identifier)
    assert pool['credit_state']=='settled' and pool['pool_remaining']==0
    retry = send()
    retry.raise_for_status()
    assert retry.json()['offset']==len(payload)
    assert raw.head_object(Bucket=store.bucket, Key=stem+'data')['ETag']==etag
    retry_pool = api.call('GET', api.prefix+'/testing/chunk-pool/'+identifier)
    assert retry_pool['transferred']==0 and retry_pool['pool_remaining']==0
    report = {'passed':True,'status':overloaded.status_code,'descendant_puts_after_expiry':0,
              'deadline_preserved':True,'started_put_drained':True,
              'body_bytes':len(payload),'meta_offset_after_expiry':0,'body_etag_preserved_on_retry':True}
    (api.root/'upload-chunk-timeout.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    from functools import partial
    from s3testsupport import ObjectStore, SERVER

    modes = {'--cancel-lock-baseline':cancellation_lock_scenario, '--cancel-lock':cancellation_lock_scenario,
             '--chunk-lock-baseline':chunk_lock_scenario, '--chunk-lock':chunk_lock_scenario,
             '--chunk-timeout':chunk_timeout_scenario}
    argument = next((name for name in modes if name in sys.argv), None)
    selected = scenario
    if argument is not None:
        sys.argv.remove(argument)
        selected = partial(modes[argument], baseline=argument.endswith('baseline'))
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    fixture = FIXTURE
    if argument == '--chunk-timeout':
        fixture += '\nfrom app.services import request_deadline\nrequest_deadline.STAGING_CLEANUP_SECONDS = 15\n'
    asyncio.run(main(selected_override=selected, fixture_override=fixture,
                     store_factory=lambda:ObjectStore(executable=runtime, volume_max=8, authenticated=True)))
