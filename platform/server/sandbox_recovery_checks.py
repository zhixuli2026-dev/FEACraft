"""Actual registered recovery and download after isolated API death. No LLM."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

import aiohttp
from sqlalchemy import select, update

from app.models import NativeRequest, NativeSession
from testsupport import SERVER


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


async def verify_recovery(api, engine, sid, proof, original_export, original_args, other_token, record):
    image_root = Path(json.loads(original_export['profile_json'])['root'])
    before = await asyncio.to_thread(sha, image_root / 'volume.ext4')
    disk_path = image_root / 'disk.json'
    original_disk = disk_path.read_bytes()
    import uuid
    broken_disk = {**json.loads(original_disk), 'uuid': str(uuid.uuid4())}
    try:
        disk_path.write_text(json.dumps(broken_disk))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as client:
            async with client.post(api.base + '/api/v1/terminal/sessions/' + sid + '/recoveries',
                                   headers={'Authorization': 'Bearer ' + api.token}) as response:
                rejected = await response.json()
                assert response.status == 502 and rejected['detail']['code'] == 'sandbox_recovery_incomplete', rejected
    finally:
        disk_path.write_bytes(original_disk)
    async with engine.connect() as db:
        failed = (await db.execute(select(NativeRequest.__table__).where(
            NativeRequest.case_id == api.case_id, NativeRequest.kind == 'terminal.recover'))).mappings().one()
    assert failed['phase'] == 'failed' and failed['result_json'] == '{}'
    assert all(c['stop']['verified'] for c in json.loads(failed['children_json'])['children'].values())
    failed_root = Path(json.loads(failed['profile_json'])['root'])
    assert not (failed_root / 'complete.json').exists()
    claim = json.loads((failed_root / 'node-claim.json').read_text())['claim']
    assert not (image_root.parent.parent / '.node-storage-budget' / claim).exists()
    await asyncio.to_thread(api.call, 'GET', '/terminal/sessions/' + sid + '/recoveries/' + failed['id'] + '/download', expect=409)
    assert await asyncio.to_thread(sha, image_root / 'volume.ext4') == before
    record('wrong_image_uuid_fails_owned_operation_without_success_or_download', 'protocol_fault')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as client:
        async with client.post(api.base + '/api/v1/terminal/sessions/' + sid + '/recoveries',
                               headers={'Authorization': 'Bearer ' + api.token}) as response:
            value = await response.json()
            (api.root / 'recovery-response.json').write_text(json.dumps(value, indent=2))
            assert response.status == 201, (response.status, value)
    assert value['phase'] == 'succeeded' and value['cleanup_verified'] is True, value
    assert not value['command_replayed'] and not value['project_files_modified']
    identifier = value['id']
    endpoint = '/terminal/sessions/' + sid + '/recoveries/' + identifier
    manifest = value['manifest']
    assert manifest['files']['Ref/inspection.txt']['sha256'] == hashlib.sha256((proof + '\n').encode()).hexdigest()
    assert 'Ref/recovery-empty' in manifest['directories'] and manifest['modes']['Ref/inspection.txt'] == 0o600
    record('registered_recovery_preserves_actual_file_empty_directory_and_mode')
    assert await asyncio.to_thread(sha, image_root / 'volume.ext4') == before == value['receipt']['image_sha256']
    record('read_only_recovery_leaves_original_image_bytes_unchanged')
    raw = await asyncio.to_thread(api.call, 'GET', endpoint + '/download', raw=True)
    assert hashlib.sha256(raw).hexdigest() == value['receipt']['archive_sha256']
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert archive.read('Ref/inspection.txt') == (proof + '\n').encode()
        assert archive.getinfo('Ref/recovery-empty/').is_dir()
        assert (archive.getinfo('Ref/inspection.txt').external_attr >> 16) & 0o777 == 0o600
    record('authenticated_download_contains_verified_original_bytes_and_metadata')
    await asyncio.to_thread(api.call, 'GET', endpoint + '/download', expect=401, extra_headers={'Authorization': ''})
    await asyncio.to_thread(api.call, 'GET', endpoint + '/download', expect=404,
                            extra_headers={'Authorization': 'Bearer ' + other_token})
    record('recovery_download_rejects_anonymous_and_other_owner')
    async with engine.connect() as db:
        session = (await db.execute(select(NativeSession.__table__).where(NativeSession.id == sid))).mappings().one()
        recovery = (await db.execute(select(NativeRequest.__table__).where(NativeRequest.id == identifier))).mappings().one()
    assert session['phase'] != 'closed' and not json.loads(session['process_json']).get('export')
    assert recovery['kind'] == 'terminal.recover' and json.loads(recovery['result_json']) == value['receipt']
    child = next(iter(json.loads(recovery['children_json'])['children'].values()))
    assert child['purpose'] == 'sandbox_recover' and child['stop']['verified'] is True
    await asyncio.to_thread(api.call, 'GET', api.prefix + '/raw?path=Ref/inspection.txt', expect=404)
    record('durable_recovery_receipt_does_not_claim_project_publication')
    # The old handle is proven terminal before restarting the same isolated API.
    api.proc.kill()
    await asyncio.to_thread(api.proc.wait, 10)
    api.proc = subprocess.Popen(original_args, cwd=api.root, env=api.env, stdout=api.log,
                               stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    async with asyncio.timeout(35):
        while True:
            assert api.proc.poll() is None
            try:
                if (await asyncio.to_thread(api.call, 'GET', '/api/health', api_prefix=False)).get('ok'):break
            except OSError:pass
            await asyncio.sleep(.1)
    again = await asyncio.to_thread(api.call, 'GET', endpoint)
    assert again['receipt'] == value['receipt']
    listing = await asyncio.to_thread(api.call, 'GET', '/terminal/sessions/' + sid + '/recoveries')
    assert {r['id']: r['phase'] for r in listing['recoveries']} == {identifier: 'succeeded', failed['id']: 'failed'}
    assert listing['next_before'] is None
    assert await asyncio.to_thread(api.call, 'GET', endpoint + '/download', raw=True) == raw
    record('restart_retains_recovery_status_and_identical_download_without_reexecution')
    selected = json.loads(recovery['profile_json'])
    path = Path(selected['root']) / 'recovery.zip'
    try:
        with path.open('r+b') as file:
            file.write(b'BAD!')
        await asyncio.to_thread(api.call, 'GET', endpoint + '/download', expect=409)
    finally:
        path.write_bytes(raw)
    record('changed_archive_refused_by_committed_hash', 'protocol_fault')
    corrupted = {**value['receipt'], 'archive_sha256': '0' * 64}
    try:
        async with engine.begin() as db:
            await db.execute(update(NativeRequest).where(NativeRequest.id == identifier).values(result_json=json.dumps(corrupted)))
        await asyncio.to_thread(api.call, 'GET', endpoint, expect=409)
    finally:
        async with engine.begin() as db:
            await db.execute(update(NativeRequest).where(NativeRequest.id == identifier).values(result_json=recovery['result_json']))
    record('changed_completion_receipt_refused', 'protocol_fault')


if __name__ == '__main__':
    import argparse
    from sandbox_recovery_inspection_checks import verify
    parser = argparse.ArgumentParser()
    parser.add_argument('--storage', choices=('fs', 's3'), default='fs')
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    args = parser.parse_args()
    def run(store=None, url=None, group=None):
        return asyncio.run(verify(store, url, 'recover'))
    def database(store):
        if args.database == 'sqlite':return run(store)
        from mixed_read_capacity_checks import postgres
        return asyncio.run(postgres(store, 'recovery', run))
    if args.storage == 'fs':database(None)
    else:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:database(store)
