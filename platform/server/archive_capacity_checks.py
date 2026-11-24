"""Real ZIP import/backup/restore, node claims and cancellation; no LLM."""
import argparse
import asyncio
import base64
import io
import json
import os
import sys
import zipfile
from urllib.parse import quote

import httpx
from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
import asyncio
import io
import threading
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from fastapi import UploadFile
from app import storage
from app.models import Workspace
from app.services import backup,case_import,node_storage_budget,owner_storage_budget
entered=threading.Event();release=threading.Event()
extracting=ContextVar('archive_extract',default=False)
settings=node_storage_budget.get_settings
def node_settings():
 value=settings()
 deny=(root/'node-zero').exists() or (extracting.get() and (root/'extract-zero').exists())
 return value.model_copy(update={'node_storage_limit_mb':0}) if deny else value
node_storage_budget.get_settings=node_settings
def boundary(stage,**fields):
 pool=owner_storage_budget.current()
 if storage._use_s3():
  if stage.startswith('backup_pack_'):
   assert pool is None, 'Read-only backup packing held an owner write grant'
  else:
   assert pool is not None and not pool.closed
 with (root/'boundaries.jsonl').open('a') as stream:
  stream.write(json.dumps({'stage':stage,'owner':pool.ticket['user_id'] if pool else None,**fields})+'\\n')
original_reserve=case_import.reserve
@contextmanager
def reserve(size, **options):
 with original_reserve(size, **options):
  claims=list((storage._fs_root()/'.node-storage-budget').glob('*.json'))
  assert claims
  boundary('extract_claim' if extracting.get() else 'upload_claim',size=size)
  yield
case_import.reserve=reserve
original_prepare=case_import._prepare_files
def prepare(archive, stage, *options):
 assert stage.resolve().is_relative_to(storage._fs_root().resolve())
 token=extracting.set(True)
 try:return original_prepare(archive, stage, *options)
 finally:extracting.reset(token)
case_import._prepare_files=prepare
original_read=zipfile.ZipExtFile.read
def read(self,*args,**kwargs):
 data=original_read(self,*args,**kwargs)
 if extracting.get() and self.name.endswith('payload.bin') and (root/'pause-import').exists():
  boundary('import_thread_paused')
  entered.set();assert release.wait(15),'import barrier timeout'
 return data
zipfile.ZipExtFile.read=read
original_pack=backup._pack_backup
def pack(*args):
 boundary('backup_pack_enter')
 result=original_pack(*args)
 if (root/'pause-backup').exists():
  entered.set();assert release.wait(15),'backup barrier timeout'
 boundary('backup_pack_exit')
 return result
backup._pack_backup=pack
@app.post('/api/v1/fixture/limits')
async def limits(body:dict,user=Depends(get_current_user)):
 backup.MAX_SOURCE_BYTES=body.get('source',100*1024*1024)
 backup.MAX_MANIFEST_BYTES=body.get('manifest',160*1024*1024)
 backup.MAX_BACKUP_BYTES=body.get('output',100*1024*1024)
 return {'ok':True}
@app.post('/api/v1/fixture/cancel/{mode}/{workspace_id}')
async def cancel(mode:str,workspace_id:int,user=Depends(get_current_user)):
 assert mode in {'import','backup'}
 marker=root/('pause-'+mode);marker.touch();entered.clear();release.clear()
 async def operation():
  async with SessionLocal() as db:
   workspace=await db.get(Workspace,workspace_id);assert workspace.user_id==user.id
   if mode=='import':
    source=UploadFile(filename='cancel.zip',file=io.BytesIO((root/'input.zip').read_bytes()))
    try:return await case_import.import_case_zip(db,workspace,user,'cancelled-import',source)
    finally:await source.close()
   return await backup.create_backup(db,workspace)
 task=asyncio.create_task(operation())
 try:
  async with asyncio.timeout(15):
   while not entered.is_set():
    if task.done():await task
    await asyncio.sleep(.01)
  task.cancel();await asyncio.sleep(.05)
  assert not task.done(),'cancel released work before actual IO drained'
  release.set()
  try:await task
  except asyncio.CancelledError:pass
  else:raise AssertionError('cancel swallowed')
  return {'cancelled':True,'drained':True}
 finally:
  release.set();marker.unlink(missing_ok=True)
  if not task.done():
   task.cancel()
   try:await task
   except asyncio.CancelledError:pass
'''


def zipped(files, compress=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compress) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return output.getvalue()


def verify(store=None, database_url=None, group='archives'):
    api = IsolatedServer('archive-capacity', app='archive_fixture:app', database_url=database_url)
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    (api.root / 'archive_fixture.py').write_text(
        'import os\nos.environ.update(' + repr(store.settings() if store else {}) + ')\n' + FIXTURE + OBSERVE, encoding='utf8')
    result = {'passed': False, 'storage': 's3' if store else 'fs',
              'database': 'postgresql' if database_url else 'sqlite', 'llm_calls': 0, 'checks': []}
    def save():
        (api.root / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    def check(name, **fields):
        result['checks'].append({'name': name, **fields}); save()
        print('ARCHIVE_PASS ' + name, flush=True)
    def credit():
        state = api.call('GET', '/fixture/credit')
        if store:
            assert state['rows'] and all(r['state'] == 'settled' and r['receipt']['closed'] for r in state['rows']), state
    def clean():
        assert not list((api.root / 'files').glob('.case-import-*'))
        assert not list((api.root / 'files/.node-storage-budget').glob('*.json'))
        credit()
    print('ARCHIVE_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            api.create_case('Archive original')
            owner = api.call('GET', '/auth/me')['id']
            workspace = api.call('GET', '/workspaces')[0]['id']
            dictionary = api.call('GET', api.prefix + '/raw?path=0-caseDict/caseDict', raw=True)
            payload = os.urandom(64 * 1024)
            source_files = {'bundle/0-caseDict/caseDict': dictionary,
                            'bundle/Ref/payload.bin': payload, 'bundle/upload.zip': b'ordinary case file named upload.zip'}
            package = zipped(source_files)
            (api.root / 'input.zip').write_bytes(package)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=120) as client:
                def importing(data=package, name='Imported', status=200):
                    response = client.post(f'/workspaces/{workspace}/cases:import',
                        files={'file': ('case.zip', data, 'application/zip')}, data={'name': name})
                    assert response.status_code == status, (response.status_code, response.text[:1200])
                    return response.json()
                def backup(status=200):
                    response = client.get(f'/workspaces/{workspace}/backup')
                    assert response.status_code == status, (response.status_code, response.text[:1200] if status != 200 else len(response.content))
                    credit()
                    return response.content
                def restore(data, status=200):
                    response = client.post('/workspaces:restore', files={'file': ('backup.zip', data, 'application/zip')})
                    assert response.status_code == status, (response.status_code, response.text[:1200])
                    credit()
                    return response.json()
                original_count = len(api.call('GET', f'/workspaces/{workspace}/cases'))
                for marker in ('node-zero', 'extract-zero'):
                    (api.root / marker).touch()
                    try:
                        refused = importing(name=marker, status=409)
                        assert refused['detail']['code'] == 'node_storage_full', refused
                    finally:
                        (api.root / marker).unlink()
                    clean()
                    assert len(api.call('GET', f'/workspaces/{workspace}/cases')) == original_count
                    check(marker + '_before_publication_cleaned')
                imported = importing()
                imported_id = imported['id']
                assert imported['files'] == 3
                for path, data in source_files.items():
                    relative = path[len('bundle/'):]
                    assert api.call('GET', f'/cases/{imported_id}/raw?path=' + quote(relative, safe=''), raw=True) == data
                clean(); check('real_zip_nested_root_binary_and_upload_filename')
                importing(status=409); clean(); check('duplicate_case_refused_and_staging_removed')
                count = len(api.call('GET', f'/workspaces/{workspace}/cases'))
                invalid = [
                    ('traversal', zipped({'../escaped': b'bad'}), 400),
                    ('parent-file', zipped({'0-caseDict/caseDict': dictionary, 'Ref': b'x', 'Ref/x': b'y'}), 400),
                    ('reserved-name', zipped({'0-caseDict/caseDict': dictionary, 'CON': b'x'}), 400),
                    ('bomb', zipped({'0-caseDict/caseDict': dictionary, 'Ref/bomb': b'0' * (2 * 1024 * 1024)}, zipfile.ZIP_DEFLATED), 413),
                ]
                damaged = bytearray(package)
                position = damaged.find(payload)
                assert position >= 0
                damaged[position] ^= 1
                invalid.append(('crc', bytes(damaged), 400))
                for name, data, status in invalid:
                    importing(data, name, status); clean()
                    assert len(api.call('GET', f'/workspaces/{workspace}/cases')) == count
                    check('invalid_zip_' + name + '_no_case_or_stage')
                cancelled = api.call('POST', f'/fixture/cancel/import/{workspace}', {})
                assert cancelled == {'cancelled': True, 'drained': True}
                clean()
                assert len(api.call('GET', f'/workspaces/{workspace}/cases')) == count
                check('cancel_during_actual_extraction_drains_then_cleans')
                if store:
                    before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                    (api.root / 'quota-zero').touch()
                    try:
                        backup(429)
                        importing(name='quota-denied', status=429)
                        assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
                    finally:
                        (api.root / 'quota-zero').unlink()
                    clean(); check('zero_owner_credit_backup_and_import_no_remote_growth')
                exported = backup()
                (api.root / 'backup.zip').write_bytes(exported)
                with zipfile.ZipFile(io.BytesIO(exported)) as archive:
                    assert archive.namelist() == ['manifest.json']
                    manifest = json.loads(archive.read('manifest.json'))
                assert len(manifest['cases']) == 2
                row = next(r for r in manifest['files'] if r['case_id'] == imported_id and r['path'] == 'Ref/payload.bin')
                assert base64.b64decode(row['data']) == payload
                assert not any(r['path'].startswith('.vibeflow/file-transactions/') for r in manifest['files'])
                check('actual_multi_case_backup_binary_and_journal_exclusion')
                for key in ('source', 'manifest', 'output'):
                    api.call('POST', '/fixture/limits', {key: 8})
                    try:
                        backup(409)
                    finally:
                        api.call('POST', '/fixture/limits', {})
                    clean(); check('backup_' + key + '_bound_refused_cleanly')
                cancelled = api.call('POST', f'/fixture/cancel/backup/{workspace}', {})
                assert cancelled == {'cancelled': True, 'drained': True}
                clean(); check('cancel_during_actual_backup_drains_then_settles')
                restored = restore(exported)
                assert restored['cases'] == 2 and restored['files'] == len(manifest['files'])
                restored_cases = api.call('GET', f'/workspaces/{restored["workspace_id"]}/cases')
                copied = next(c for c in restored_cases if c['name'] == 'Imported')
                assert api.call('GET', f'/cases/{copied["id"]}/raw?path=Ref/payload.bin', raw=True) == payload
                clean(); check('actual_backup_restore_preserves_binary_and_case_metadata')
                restore(b'invalid ZIP', 400); clean(); check('invalid_restore_releases_credit')
                if store:
                    before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                    (api.root / 'quota-zero').touch()
                    try:
                        restore(exported, 429)
                        assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
                    finally:
                        (api.root / 'quota-zero').unlink()
                    clean(); check('zero_owner_credit_restore_no_remote_growth')
                events = [json.loads(line) for line in (api.root / 'boundaries.jsonl').read_text().splitlines()]
                assert {'upload_claim', 'extract_claim', 'import_thread_paused', 'backup_pack_enter', 'backup_pack_exit'} <= {r['stage'] for r in events}
                assert all(r['owner'] == (owner if store else None) for r in events)
                check('observed_real_claims_and_owner_through_io', events=len(events))
                result['passed'] = True; save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:2400]}; save()
        print('ARCHIVE_FAILED ' + json.dumps(result['failure']), flush=True)
        return 1
    print('ARCHIVE_OK ' + str(api.root / 'checks.json'), flush=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--storage', choices=('fs', 's3'), default='s3')
    args = parser.parse_args()
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
            code = asyncio.run(postgres(store, 'archives', verify)) if args.database == 'postgresql' else verify(store)
    else:
        code = asyncio.run(postgres(None, 'archives', verify)) if args.database == 'postgresql' else verify()
    sys.exit(code)
