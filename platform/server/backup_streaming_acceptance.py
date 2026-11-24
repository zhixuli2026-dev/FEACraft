"""Actual FS/S3 v1 backup memory, byte compatibility and restore; no LLM."""
import argparse
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import httpx
import testsupport
from testsupport import IsolatedServer, SERVER
from terminal_shutdown_acceptance import RUNNER
from app.services import process_identity_native as identity

ROOT = SERVER.parents[1]
FIXTURE = '''
import copy,gc,hashlib,io,json,os,tracemalloc
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.models import Case,Workspace
from app.services import backup
from app.services.process_identity_native import observe
root=Path(__file__).parent
@app.on_event('startup')
async def birth():
 (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())))
@app.post('/fixture/seed/{case_id}')
async def seed(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await db.get(Case,case_id);workspace=await db.get(Workspace,case.workspace_id)
 if workspace.user_id!=user.id:raise HTTPException(404)
 block=bytes(range(251))*4178
 payload=(block*25)[:24*1024*1024+5]
 files={'Ref/large.bin':payload,'Ref/empty.bin':b'','Ref/one.bin':b'\\xff',
        'Ref/two.bin':b'\\x00\\xfe','Ref/中文🙂.bin':bytes(range(256))*4096+b'end'}
 result={}
 for path,data in files.items():
  storage.put_bytes(case.s3_prefix,path,data)
  result[path]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
 (root/'seed-prefix').write_text(case.s3_prefix)
 return result
original=backup._pack_backup
def pack(manifest,references,manifests,output=None):
 if (root/'mutate-source').exists():
  storage.put_bytes(references[0][1],'Ref/large.bin',b'actual changed source')
 if not (root/'compare-memory').exists():return original(manifest,references,manifests,output)
 (root/'compare-memory').unlink()
 namespace={'__name__':'app.services.backup_baseline','__package__':'app.services'}
 exec(compile((root/'backup_baseline.py').read_text('utf8'),'backup_baseline.py','exec'),namespace)
 baseline_manifest=copy.deepcopy(manifest)
 gc.collect();tracemalloc.start()
 old=namespace['_pack_backup'](baseline_manifest,references,manifests)
 _,old_peak=tracemalloc.get_traced_memory();tracemalloc.stop()
 (root/'baseline.zip').write_bytes(old)
 del old,baseline_manifest;gc.collect()
 tracemalloc.start()
 current=original(manifest,references,manifests,output)
 _,new_peak=tracemalloc.get_traced_memory();tracemalloc.stop()
 (root/'memory.json').write_text(json.dumps({'old_peak_bytes':old_peak,'new_peak_bytes':new_peak,
  'file_rows':len(manifest['files']),'no_retained_payloads':all('data' not in row for row in manifest['files'])}))
 return current
backup._pack_backup=pack
'''


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ['manifest.json']
        return json.loads(archive.read('manifest.json'))


def packed(manifest):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    return output.getvalue()


def verify(backend):
    api = IsolatedServer('backup-streaming-' + backend, app='backup_stream_fixture:app')
    result = {'passed': False, 'backend': backend, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'checks': [], 'api_exits': []}
    store = None
    original_popen = subprocess.Popen

    def save():
        (api.root / 'backup-streaming-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf8')

    def launch(argv, **kwargs):
        assert argv[1:3] == ['-m', 'uvicorn']
        return original_popen([argv[0], '-X', 'utf8', '-B', str(api.root / 'graceful_server.py'), *argv[3:]], **kwargs)

    def stop():
        birth = json.loads((api.root / 'api-birth.json').read_text('utf8'))
        (api.root / 'graceful-stop').touch()
        api.proc.wait(90)
        observed = identity.probe(birth)
        assert api.proc.returncode == 0 and observed['state'] == 'exited'
        result['api_exits'].append({'identity': birth, 'observed': observed})
        api.log.close()

    try:
        settings = {}
        if backend == 's3':
            from s3testsupport import ObjectStore
            store = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed')
            store.__enter__()
            settings = store.settings()
            result['store_evidence'] = str(store.root)
        baseline = ROOT / 'tmp/backup-streaming-baseline-20261117.py'
        (api.root / 'backup_baseline.py').write_bytes(baseline.read_bytes())
        (api.root / 'graceful_server.py').write_text(RUNNER, 'utf8')
        (api.root / 'backup_stream_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, 'utf8')
        testsupport.subprocess.Popen = launch
        try:
            api.__enter__()
        finally:
            testsupport.subprocess.Popen = original_popen
        case = api.create_case('Streaming backup')
        workspace = api.call('GET', '/workspaces')[0]['id']
        expected = api.call('POST', '/fixture/seed/' + str(case['id']), {}, api_prefix=False)
        (api.root / 'compare-memory').touch()
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          timeout=180, trust_env=False) as client:
            response = client.get(f'/workspaces/{workspace}/backup')
            assert response.status_code == 200, response.text[:1000]
            data = response.content
            (api.root / 'current.zip').write_bytes(data)
            manifest = unpack(data)
            old_manifest = unpack((api.root / 'baseline.zip').read_bytes())
            assert old_manifest == manifest
            memory = json.loads((api.root / 'memory.json').read_text('utf8'))
            result['memory'] = memory
            save()
            assert memory['no_retained_payloads'] and memory['new_peak_bytes'] < memory['old_peak_bytes'] * .5
            result['checks'].append('actual_old_and_new_pack_same_manifest_with_less_than_half_traced_peak')
            import base64
            for path, info in expected.items():
                row = next(row for row in manifest['files'] if row['path'] == path)
                content = base64.b64decode(row['data'], validate=True)
                assert len(content) == info['size'] and hashlib.sha256(content).hexdigest() == info['sha256'] == row['sha256']
            result['checks'].append('large_binary_empty_remainder_and_unicode_paths_exact')
            restored = client.post('/workspaces:restore', files={'file': ('backup.zip', data, 'application/zip')})
            assert restored.status_code == 200, restored.text[:1000]
            new_workspace = restored.json()['workspace_id']
            new_case = api.call('GET', f'/workspaces/{new_workspace}/cases')[0]['id']
            for path, info in expected.items():
                raw = client.get(f'/cases/{new_case}/raw', params={'path': path})
                assert raw.status_code == 200 and len(raw.content) == info['size']
                assert hashlib.sha256(raw.content).hexdigest() == info['sha256']
            result['checks'].append('actual_public_restore_reads_exact_original_files')
            # Legacy v1 archives remain accepted independently of the new writer.
            restored_old = client.post('/workspaces:restore', files={'file': ('legacy.zip', (api.root / 'baseline.zip').read_bytes(), 'application/zip')})
            assert restored_old.status_code == 200, restored_old.text[:1000]
            result['checks'].append('old_writer_v1_archive_still_restores')
            count = len(api.call('GET', '/workspaces'))
            mutations = ('hash', 'base64', 'path', 'missing_dictionary', 'metadata')
            for mode in mutations:
                damaged = copy.deepcopy(manifest)
                row = next(row for row in damaged['files'] if row['path'] == 'Ref/one.bin')
                if mode == 'hash': row['sha256'] = '0' * 64
                elif mode == 'base64': row['data'] = '!invalid!'
                elif mode == 'path': row['path'] = '../outside'
                elif mode == 'missing_dictionary': damaged['files'] = [r for r in damaged['files'] if r['path'] != '0-caseDict/caseDict']
                elif mode == 'metadata': damaged['cases'][0]['case_meta'] = '{"changed":true}'
                refused = client.post('/workspaces:restore', files={'file': ('invalid.zip', packed(damaged), 'application/zip')})
                assert refused.status_code == 400, (mode, refused.status_code, refused.text[:500])
                assert len(api.call('GET', '/workspaces')) == count
                result['checks'].append('invalid_' + mode + '_refused_before_workspace_creation')
            (api.root / 'mutate-source').touch()
            try:
                stale = client.get(f'/workspaces/{workspace}/backup')
                assert stale.status_code == 409, (stale.status_code, stale.text[:1000])
            finally:
                (api.root / 'mutate-source').unlink()
            result['checks'].append('actual_source_change_after_capture_returns_conflict_without_zip')
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:1800]}
        raise
    finally:
        testsupport.subprocess.Popen = original_popen
        try:
            if api.proc is not None and api.proc.poll() is None:
                stop()
        finally:
            if store is not None:
                store.__exit__(None, None, None)
                result['store_stopped'] = store.proc.poll() is not None
            save()
    print(json.dumps({'passed': result['passed'], 'evidence': str(api.root), 'checks': len(result['checks']), 'memory': result['memory']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--backend', choices=('fs', 's3'), required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text('utf8'))
    assert review['entry'] == 'platform/server/backup_streaming_acceptance.py'
    for path, digest in review['sources'].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    verify(args.backend)
