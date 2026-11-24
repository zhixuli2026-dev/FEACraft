"""Restore a legacy v1 archive through HTTP and measure the two actual readers."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import random
import time
import zipfile

import httpx

from live_llm_job_repeat_acceptance import main


FIXTURE = '''
import gc, json, time, tracemalloc
from pathlib import Path
from app import storage
from app.config import get_settings
from app.services.backup_manifest import load_manifest
from app.services.backup_restore import RestoreFiles
root = Path(__file__).parent
original_load, original_close = RestoreFiles.load, RestoreFiles.close

def observed_load(self, upload):
    if (root/'limit-restore').exists():
        settings = get_settings()
        previous = settings.node_storage_limit_mb
        try:
            settings.node_storage_limit_mb = 1
            return original_load(self, upload)
        finally:
            settings.node_storage_limit_mb = previous
    if not (root/'measure-restore').exists():
        return original_load(self, upload)
    gc.collect()
    tracemalloc.start()
    try:
        old = load_manifest(upload, retain_encoded=False)
        before = tracemalloc.get_traced_memory()[1]
        sizes = [len(row['_bytes']) for row in old['files']]
        del old
        gc.collect()
        tracemalloc.reset_peak()
        manifest = original_load(self, upload)
        peak = tracemalloc.get_traced_memory()[1]
        assert sizes == [row['_size'] for row in manifest['files']]
        assert all('data' not in row and '_bytes' not in row for row in manifest['files'])
        (root/'restore-peaks.json').write_text(json.dumps(dict(old_bytes=before,
            staged_bytes=peak, reserved=self._reserved, total_source_bytes=sum(sizes))))
    finally:
        tracemalloc.stop()
    (root/'restore-validated').touch()
    deadline = time.monotonic() + 60
    while (root/'measure-restore').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Concurrent real write did not finish')
        time.sleep(.04)
    return manifest

def observed_close(self):
    original_close(self)
    claims = storage._fs_root()/'.node-storage-budget'
    (root/'restore-closed.json').write_text(json.dumps(dict(closed=self._stream.closed,
        claims_left=len(list(claims.glob('*.json'))))))

RestoreFiles.load, RestoreFiles.close = observed_load, observed_close
'''


def archive(manifest, encoding='utf-8'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        entry = zipfile.ZipInfo('manifest.json', date_time=(2020, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        bundle.writestr(entry, json.dumps(manifest, ensure_ascii=False).encode(encoding))
    return output.getvalue()


def scenario(api, *, store=None, **_):
    rng = random.Random(20261118)
    contents = {(cid, '0-caseDict/caseDict'): b'workflowState A;\n' for cid in (10, 20)}
    for number in range(12):
        contents[(10 if number % 2 else 20, f'Ref/{number:02}.bin')] = rng.randbytes(1024*1024)
    contents[20, 'Ref/空.txt'] = b''
    files = [dict(data=base64.b64encode(data).decode(), path=path, case_id=cid,
                  sha256=hashlib.sha256(data).hexdigest()) for (cid, path), data in contents.items()]
    cases = [dict(id=cid, name=f'旧算例{cid}', workflow_state='A', dict_revision=1,
                  case_meta='{}', parse_status='ok') for cid in (10, 20)]
    manifest = dict(files=files, cases=cases, schema='vibeflow.workspace-backup.v1',
                    workspace={'name':'旧版工作区'})
    data = archive(manifest)
    measure = api.root/'measure-restore'
    receipt = api.root/'restore-closed.json'

    def cleaned():
        result = json.loads(receipt.read_text())
        assert result == {'closed': True, 'claims_left': 0}, result
        receipt.unlink()

    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=150, trust_env=False) as client:
        def restore(payload):
            return client.post('/workspaces:restore', files={'file':('old.zip', payload, 'application/zip')})

        measure.touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(restore, data)
            try:
                deadline = time.monotonic() + 75
                while not (api.root/'restore-validated').exists():
                    if future.done():
                        raise AssertionError(f'Restore ended before gate: {future.result().status_code}')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Restore validation did not complete')
                    time.sleep(.05)
                api.put('Ref/during-restore.txt', 'same owner wrote while restore waited')
            finally:
                measure.unlink()
            restored = future.result()
        assert restored.status_code == 200, restored.text[:400]
        cleaned()
        peaks = json.loads((api.root/'restore-peaks.json').read_text())
        assert peaks['staged_bytes'] < peaks['old_bytes'] // 2
        assert peaks['reserved'] >= peaks['total_source_bytes']
        result = restored.json()
        assert result['cases'] == 2 and result['files'] == len(files), result
        restored_cases = api.call('GET', f'/workspaces/{result["workspace_id"]}/cases')
        ids = {case['name']:case['id'] for case in restored_cases}
        for (original, path), content in contents.items():
            response = client.get(f'/cases/{ids[f"旧算例{original}"]}/raw', params={'path':path})
            assert response.status_code == 200 and response.content == content, path
        before = api.call('GET', '/workspaces')
        bad = restore(data[:-24])
        assert bad.status_code == 400, bad.text[:400]
        cleaned()
        files[-1]['sha256'] = '0'*64
        bad = restore(archive(manifest))
        assert bad.status_code == 400 and 'hash' in bad.json()['detail'], bad.text[:400]
        cleaned()
        limit = api.root/'limit-restore'
        limit.touch()
        try:
            full = restore(data)
            assert full.status_code == 409 and full.json()['detail']['code'] == 'node_storage_full', full.text[:400]
            cleaned()
        finally:
            limit.unlink()
        assert api.call('GET', '/workspaces') == before
        small = {**manifest, 'files':files[:2]}
        small['cases'] = [{**case, 'workflow_state':'B'} for case in cases]
        mismatch = restore(archive(small, 'utf-16'))
        assert mismatch.status_code == 400 and 'caseDict' in mismatch.json()['detail'], mismatch.text[:400]
        cleaned()
        assert api.call('GET', '/workspaces') == before
        small['cases'] = cases
        retry = restore(archive(small, 'utf-16'))
        assert retry.status_code == 200 and retry.json()['files'] == 2, retry.text[:400]
        cleaned()
    evidence = dict(backend='s3' if store else 'fs', measured=peaks,
        archive_sha256=hashlib.sha256(data).hexdigest(), archive_bytes=len(data),
        verified_files=len(files), verified_cases=2, same_owner_write_during_validation=True,
        bad_zip_http=400, wrong_digest_http=400, canonical_mismatch_http=400, capacity_http=409,
        failed_restores_added_no_workspace=True, final_claims=0, utf16_retry=True)
    (api.root/'backup-restore-memory.json').write_text(json.dumps(evidence, indent=2))
    return evidence


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE, store_volume_max=6))
