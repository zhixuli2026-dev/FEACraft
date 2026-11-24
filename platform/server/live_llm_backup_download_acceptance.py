"""Download and restore an actual disk-backed ZIP; verify failed and interrupted transfers."""
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import time
import zipfile

import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_failure_fallback_acceptance import kill_owned_api
from live_llm_outbox_restart_acceptance import restart
from live_llm_publication_restart_acceptance import worker_peer


FIXTURE = '''
import asyncio, gc, hashlib, json, os, tracemalloc
from pathlib import Path
from app.config import get_settings
from app.services import backup
from app.services.backup_download import BackupDownload
from app import storage
root = Path(__file__).parent
(root/'backup-api-pid').write_text(str(os.getpid()))
original_pack = backup._pack_backup
original_write = BackupDownload.write
original_chunks = BackupDownload.chunks
original_close = BackupDownload.close

def observed_pack(manifest, refs, manifests, output=None):
    if (root/'test-zip-limit').exists():
        previous = backup.MAX_BACKUP_BYTES
        try:
            backup.MAX_BACKUP_BYTES = 5 * 1024 * 1024
            return original_pack(manifest, refs, manifests, output)
        finally:
            backup.MAX_BACKUP_BYTES = previous
    if (root/'change-after-pack').exists():
        result = original_pack(manifest, refs, manifests, output)
        storage.put_bytes(refs[0][1], 'Ref/late-file.txt', b'actual concurrent change')
        return result
    if not (root/'measure-download').exists():
        return original_pack(manifest, refs, manifests, output)
    (root/'measure-download').unlink()
    gc.collect()
    tracemalloc.start()
    try:
        buffered = original_pack(manifest, refs, manifests)
        baseline_peak = tracemalloc.get_traced_memory()[1]
        digest = hashlib.sha256(buffered).hexdigest()
        del buffered
        gc.collect()
        tracemalloc.reset_peak()
        length = original_pack(manifest, refs, manifests, output)
        disk_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    (root/'download-peaks.json').write_text(json.dumps(dict(buffered=baseline_peak, disk=disk_peak,
        archive_bytes=length, reserved=output._reserved, sha256=digest)))
    return length

def observed_write(self,data):
    settings = get_settings()
    previous = settings.node_storage_limit_mb
    if self._reserved and (root/'deny-next-growth').exists():
        settings.node_storage_limit_mb = 1
    try:
        return original_write(self,data)
    finally:
        settings.node_storage_limit_mb = previous

async def observed_chunks(self):
    async for block in original_chunks(self):
        yield block
        if (root/'slow-download').exists():
            (root/'active-spool').write_text(str(self._stream.name))
            (root/'first-sent').touch()
            while (root/'slow-download').exists():
                await asyncio.sleep(.02)

def observed_close(self):
    original_close(self)
    claims = storage._fs_root()/'.node-storage-budget'
    (root/'download-closed.json').write_text(json.dumps(dict(closed=self._stream.closed,
        reserved=self._reserved, claims_left=len(list(claims.glob('*.json'))))))

backup._pack_backup = observed_pack
BackupDownload.write = observed_write
BackupDownload.chunks = observed_chunks
BackupDownload.close = observed_close
'''


def scenario(api, *, store=None, **_):
    workspace = api.call('GET', '/workspaces')[0]['id']
    url = f'/workspaces/{workspace}/backup'
    payload = os.urandom(12 * 1024 * 1024)
    path = 'Ref/incompressible.bin'
    closed = api.root / 'download-closed.json'

    def wait(check):
        deadline = time.monotonic() + 30
        while not check():
            if time.monotonic() > deadline:
                raise TimeoutError('Download observation did not arrive')
            time.sleep(.05)

    def receipt():
        wait(lambda: closed.exists() and closed.stat().st_size)
        value = json.loads(closed.read_text())
        assert value['closed'] and value['claims_left'] == 0, value
        closed.unlink()
        return value['reserved']

    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=150, trust_env=False) as client:
        small = client.get(url)
        assert small.status_code == 200 and zipfile.is_zipfile(io.BytesIO(small.content)) and receipt() == 0
        uploaded = client.post(api.prefix+'/upload', params={'path':path},
                               files={'file':('data.bin',payload,'application/octet-stream')})
        assert uploaded.status_code == 200, uploaded.text[:500]
        (api.root/'measure-download').touch()
        response = client.get(url)
        assert response.status_code == 200
        reserved = receipt()
        peaks = json.loads((api.root/'download-peaks.json').read_text())
        assert len(response.content) == int(response.headers['Content-Length']) == peaks['archive_bytes']
        assert len(response.content) <= reserved < len(response.content) + 4 * 1024 * 1024
        assert peaks['disk'] < peaks['buffered'] and hashlib.sha256(response.content).hexdigest() == peaks['sha256']
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
        row, = (r for r in manifest['files'] if r['path'] == path)
        assert base64.b64decode(row['data'], validate=True) == payload
        restored = client.post('/workspaces:restore', files={'file':('actual.zip',response.content,'application/zip')})
        assert restored.status_code == 200, restored.text[:500]
        clone, = api.call('GET', f'/workspaces/{restored.json()["workspace_id"]}/cases')
        assert client.get(f'/cases/{clone["id"]}/raw', params={'path':path}).content == payload

        deny = api.root/'deny-next-growth'
        deny.touch()
        try:
            failure = client.get(url)
            assert failure.status_code == 409 and failure.json()['detail']['code'] == 'node_storage_full'
            failed_reservation = receipt()
            assert 0 < failed_reservation < reserved
        finally:
            deny.unlink()
        limited = api.root/'test-zip-limit'
        limited.touch()
        try:
            too_large = client.get(url)
            assert too_large.status_code == 409 and too_large.json()['detail'] == '备份超过 100 MiB'
            assert 0 < receipt() <= 5 * 1024 * 1024
        finally:
            limited.unlink()

        slow = api.root/'slow-download'
        slow.touch()
        try:
            with client.stream('GET',url) as partial:
                body = partial.iter_bytes()
                assert partial.status_code == 200 and next(body)
                wait((api.root/'first-sent').exists)
                with client.stream('GET',url) as busy:
                    assert busy.status_code == 429, busy.status_code
                    busy.read()
                    assert busy.json()['detail']['code']=='archive_busy'
            disconnected_reservation = receipt()
            assert disconnected_reservation == reserved
        finally:
            slow.unlink()
        # Terminate only the isolated owner while its checked ZIP is still open.
        with worker_peer(api, store) as peer:
            (api.root/'first-sent').unlink()
            slow.touch()
            try:
                with client.stream('GET',url) as partial:
                    body = partial.iter_bytes()
                    assert partial.status_code == 200 and next(body)
                    wait((api.root/'first-sent').exists)
                    files = api.root/'files'
                    spool = Path((api.root/'active-spool').read_text()).resolve()
                    assert spool.parent == files.resolve() and spool.stat().st_size == int(partial.headers['content-length'])
                    claims = [json.loads(p.read_text()) for p in (files/'.node-storage-budget').glob('*.json')]
                    pid = int((api.root/'backup-api-pid').read_text())
                    owned = [claim for claim in claims if claim['owner']['pid']==pid]
                    assert sum(v['bytes'] for v in owned) == reserved
                    assert peer.call('GET',url,expect=429)['detail']['code']=='archive_busy'
                    kill_owned_api(api,pid)
                    assert not spool.exists() and not closed.exists()
                peer_archive = peer.call('GET',url,raw=True)
                with zipfile.ZipFile(io.BytesIO(peer_archive)) as archive:
                    assert json.loads(archive.read('manifest.json'))['files'] == manifest['files']
                assert all(not (files/'.node-storage-budget'/(v['id']+'.json')).exists() for v in owned)
            finally:
                slow.unlink()
                if api.proc.poll() is not None:
                    restart(api)
        retried = client.get(url)
        assert retried.status_code == 200 and receipt() == reserved
        with zipfile.ZipFile(io.BytesIO(retried.content)) as archive:
            assert json.loads(archive.read('manifest.json'))['files'] == manifest['files']
        change = api.root/'change-after-pack'
        change.touch()
        try:
            stale = client.get(url)
            assert stale.status_code == 409 and '源文件清单已变化' in stale.json()['detail']
            assert receipt() == reserved
        finally:
            change.unlink()
        with zipfile.ZipFile(io.BytesIO(client.get(url).content)) as archive:
            final = json.loads(archive.read('manifest.json'))
            late, = (r for r in final['files'] if r['path']=='Ref/late-file.txt')
            assert base64.b64decode(late['data'], validate=True)==b'actual concurrent change'
        assert receipt() == reserved
    evidence = dict(storage='s3' if store else 'fs', measured=peaks, restored_bytes=len(payload),
                    small_archive_disk_bytes=0, node_full_http=409, refused_after_reserved=failed_reservation,
                    concurrent_http=429, disconnect_released=disconnected_reservation, final_claims=0,
                    owner_exit_deleted_spool=True, dead_owner_claims_reconciled=len(owned),
                    peer_downloaded_after_exit=True, restarted_owner_downloaded=True,
                    compressed_limit_http=409, post_pack_source_changed_http=409, new_file_in_retry=True)
    (api.root/'backup-download.json').write_text(json.dumps(evidence,indent=2))
    return evidence


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE, store_volume_max=6))
