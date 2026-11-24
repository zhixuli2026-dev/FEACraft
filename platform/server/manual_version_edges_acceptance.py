"""Actual FS/S3 versions, caseDict edits, source changes and upload pages.

No provider or model reply is involved. Native exits affect only owned APIs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import traceback
import uuid

from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore

ROOT = SERVER.parents[1]
RUNTIME = ROOT / 'third_party/seaweed-4.52-runtime/weed'
APP = '''import json,os,uuid
from pathlib import Path
settings=Path(__file__).parent/'isolated-store.json'
if settings.exists():os.environ.update(json.loads(settings.read_text()))
from fastapi import Depends
from app.main import app
from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import manual_file_recovery as manual
from app.services.process_identity_native import observe

@app.get('/api/v1/test/version-birth')
async def birth():return observe(os.getpid())

@app.post('/api/v1/test/version-source/{case_id}')
async def source(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        identifier=str(uuid.uuid4())
        source=('objects/' if storage._use_s3() else 'preparing-')+identifier
        path=manual.source_path(source)
        for name,data in [('before/0',body.get('content','ACTUAL_RETAINED_VERSION').encode()),('manifest.json',b'partial-manifest')]:
            if storage._use_s3():
                from app.services.attempt_objects import put_new
                put_new(case.s3_prefix,path+'/'+name,data)
            else:
                file=storage._fs_path(case.s3_prefix,path+'/'+name)
                file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
        return {'source':source}

@app.post('/api/v1/test/version-change/{case_id}')
async def change(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        path=manual.source_path(body['source'])+'/before/0'
        if storage._use_s3():
            from app.services.attempt_objects import arguments
            storage.s3().put_object(**arguments(case.s3_prefix,path),Body=b'ACTUAL_SOURCE_CHANGED')
        else:storage._fs_path(case.s3_prefix,path).write_bytes(b'ACTUAL_SOURCE_CHANGED')
        return {'changed':True}

@app.post('/api/v1/test/version-arm')
async def arm(user=Depends(get_current_user)):
    (Path.cwd()/'isolation-exit').touch();return {'armed':True}

def with_exit(original):
    def isolate(*args,**kwargs):
        result=original(*args,**kwargs)
        marker=Path.cwd()/'isolation-exit'
        if marker.exists():marker.unlink();os._exit(0)
        return result
    return isolate
if storage._use_s3():
    from app.services import manual_file_recovery_s3
    manual_file_recovery_s3.isolate=with_exit(manual_file_recovery_s3.isolate)
else:manual.isolate_local=with_exit(manual.isolate_local)
'''


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args, store=None):
    from app.services.windows_process_scope import ProcessHandle
    api = IsolatedServer('manual-version-S3' if store else 'manual-version-FS', app='version_fixture:app')
    (api.root / 'version_fixture.py').write_text(APP, encoding='utf8')
    if store: (api.root / 'isolated-store.json').write_text(json.dumps(store.settings()), encoding='utf8')
    report = {'passed': False, 'backend': 'S3+SQLite' if store else 'FS+SQLite', 'real_model_calls': 0,
              'mock_llm_calls': 0, 'review_sha256': sha(args.review), 'checks': [], 'API_exits': []}
    handles = []; entered = False

    def start():
        nonlocal entered
        api.__enter__(); entered = True
        birth = api.call('GET', '/test/version-birth')
        handles.append((ProcessHandle.query(birth), birth))

    def seed(content='ACTUAL_RETAINED_VERSION'):
        return api.call('POST', '/test/version-source/' + str(api.case_id), {'content': content})['source']

    def options(source): return api.call('GET', api.prefix + '/file-recovery?source=' + source)
    def body(plan, action, **extra):
        return dict(action=action, source=plan.get('source', 'pending'), snapshot_sha256=plan['snapshot_sha256'],
                    expected_revision=plan['expected_revision'], expected_event_sequence=plan['expected_event_sequence'],
                    note='Actual version boundary ' + action, **extra)
    def send(value, expect=202):
        return api.call('POST', api.prefix + '/file-recovery/actions', value,
                        extra_headers={'Idempotency-Key': str(uuid.uuid4())}, expect=expect)
    def continue_original(ticket):
        original = api.call('GET', api.prefix + '/file-recovery/actions/' + ticket)
        return api.call('POST', api.prefix + '/file-recovery/actions/' + ticket + ':continue',
                        {'confirmed': True, 'request_sha256': original['confirmation']['request_sha256']}, expect=202)
    def counts():
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            return {name: db.execute(sql, (api.case_id,)).fetchone()[0] for name, sql in [
                ('requests', "SELECT COUNT(*) FROM durable_events WHERE case_id=? AND type='files.manual_recovery.requested'"),
                ('unsettled', "SELECT COUNT(*) FROM case_storage_operations WHERE case_id=? AND state!='settled'")]}

    try:
        start(); api.create_case('Actual manual version boundaries')
        email = api.call('GET', '/auth/me')['email']
        sources = [seed() for _ in range(5)]
        observed = []; cursor = ''
        while True:
            page = api.call('GET', api.prefix + '/file-transactions?include_preparations=true&preparation_limit=2&preparation_after=' + cursor)['preparations']
            observed += [row['transaction_id'] for row in page['entries']]
            if not page['has_more']: break
            cursor = page['next_cursor']
        expected = {source.removeprefix('objects/').removeprefix('preparing-') for source in sources}
        assert len(observed) == 5 and set(observed) == expected
        report['checks'].append({'kind': 'actual_preparation_paging', 'versions': 5, 'pages': 3})
        stale = body(options(sources[0]), 'isolate_version'); before = counts()
        api.call('POST', '/test/version-change/' + str(api.case_id), {'source': sources[0]})
        assert send(stale, expect=409)['detail']['code'] == 'input_stale'
        assert counts() == before
        report['checks'].append({'kind': 'changed_source_refused_before_request_or_reservation', 'counts_unchanged': True})
        original = api.call('GET', api.prefix + '/raw?path=0-caseDict/caseDict', raw=True).decode('utf8')
        chosen = original + '\n// Actual manually selected version\n'
        isolated = send(body(options(seed(chosen)), 'isolate_version'))
        plan = api.call('GET', api.prefix + '/file-recovery/actions/' + isolated['id'] + '/restore-plan?path=before/0')
        version = api.call('GET', api.prefix + '/file:version?path=0-caseDict/caseDict')
        before_case = api.call('GET', api.prefix)
        restored = send(body(plan, 'restore_file', isolation_id=isolated['id'], file_path='before/0',
                             target_path='0-caseDict/caseDict', expected_target_sha256=version['content_sha256']))
        after_case = api.call('GET', api.prefix)
        assert restored['result']['new_edit'] is True
        assert after_case['dict_revision'] == before_case['dict_revision'] + 1
        assert after_case['workflow_state'] == before_case['workflow_state'] == 'A' and after_case['parse_status'] == 'ok'
        assert api.call('GET', api.prefix + '/raw?path=0-caseDict/caseDict', raw=True).decode('utf8') == chosen
        assert continue_original(restored['id']) == restored
        report['checks'].append({'kind': 'caseDict_selected_as_new_validated_edit', 'revision_before': before_case['dict_revision'],
                                 'revision_after': after_case['dict_revision'], 'same_receipt': True})
        bad = chosen.replace('workflowState A;', 'workflowState B;')
        isolated_bad = send(body(options(seed(bad)), 'isolate_version'))
        plan = api.call('GET', api.prefix + '/file-recovery/actions/' + isolated_bad['id'] + '/restore-plan?path=before/0')
        version = api.call('GET', api.prefix + '/file:version?path=0-caseDict/caseDict')
        before = counts()
        send(body(plan, 'restore_file', isolation_id=isolated_bad['id'], file_path='before/0',
                  target_path='0-caseDict/caseDict', expected_target_sha256=version['content_sha256']), expect=409)
        assert counts() == before, {'before': before, 'after': counts()}
        assert api.call('GET', api.prefix)['dict_revision'] == after_case['dict_revision']
        assert api.call('GET', api.prefix + '/raw?path=0-caseDict/caseDict', raw=True).decode('utf8') == chosen
        report['checks'].append({'kind': 'caseDict_stage_bypass_refused_without_pending_operation'})
        uploads = []
        for index in range(5):
            item = api.call('POST', api.prefix + '/uploads', {'path': 'Ref/page-' + str(index), 'size': 1, 'expected_sha256': None})
            uploads.append(item['id'])
            p = api.call('GET', api.prefix + '/upload-recovery/' + item['id'])
            api.call('POST', api.prefix + '/upload-recovery/actions', {'action': 'retain_for_review', 'upload_id': item['id'],
                'snapshot_sha256': p['snapshot_sha256'], 'note': 'Actual paged upload', 'source_blocks': None, 'prefix_blocks': None},
                extra_headers={'Idempotency-Key': str(uuid.uuid4())}, expect=202)
        for suffix in ['', '/actions']:
            identifiers = []; cursor = None; pages = 0
            while True:
                page = api.call('GET', api.prefix + '/upload-recovery' + suffix + '?limit=2' + ('' if cursor is None else '&cursor=' + str(cursor)))
                identifiers += [row['upload_id'] if suffix else row['id'] for row in page['entries']]; pages += 1
                if not page['has_more']: break
                cursor = page['next_cursor']
            assert len(identifiers) == 5 and set(identifiers) == set(uploads)
            report['checks'].append({'kind': 'actual_upload_history_paging' if suffix else 'actual_upload_discovery_paging', 'uploads': 5, 'pages': pages})
        api.call('POST', '/test/version-arm', {})
        try: send(body(options(sources[1]), 'isolate_version'))
        except OSError: pass
        else: raise AssertionError('Expected actual API death after isolation')
        handle, birth = handles[-1]; handle.wait_exit(15)
        report['API_exits'].append({'birth': birth, 'proof': 'retained_creation_handle'})
        api.__exit__(None, None, None); entered = False
        shutil.copyfile(api.root / 'api.log', api.root / 'api-before-exit.log')
        start(); api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
        pending = api.call('GET', api.prefix + '/file-recovery/actions-page?limit=1')['items'][0]
        assert pending['state'] == 'pending'
        result = continue_original(pending['id'])
        assert result['state'] == 'completed' and continue_original(pending['id']) == result
        assert counts()['unsettled'] == 0
        report['checks'].append({'kind': 'new_API_continues_original_ticket_after_physical_isolation', 'ticket': result['id']})
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1800],
                             'frames': [{'file': Path(row.filename).name, 'line': row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
            handle, birth = handles[-1]; handle.wait_exit(15)
            report['API_exits'].append({'birth': birth, 'proof': 'retained_creation_handle'})
        for handle, _ in handles: handle.close()
        report['passed'] = bool(report.get('functional_passed') and len(report['API_exits']) == 2)
        (api.root / 'edges-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('MANUAL_VERSION_EDGES_RESULT ' + str(api.root / 'edges-result.json'), flush=True)
    return report, api.root


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--backend', choices=['FS', 'S3'], required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    for name, digest in review['source_hashes'].items(): assert sha(ROOT / name) == digest, name
    if args.backend == 'FS':
        report, _ = verify(args)
    else:
        with ObjectStore(executable=RUNTIME) as store:
            report, root = verify(args, store)
            (root / 'store-reference.json').write_text(json.dumps({'root': str(store.root), 'runtime': str(RUNTIME)}), encoding='utf8')
        probe = "import json,pathlib,sys; o=json.loads(pathlib.Path(sys.argv[1]).read_text()); p=pathlib.Path('/proc')/str(o['pid']); assert not p.exists() or p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]!=o['start']; print('ORIGINAL_STORE_BIRTH_ABSENT')"
        result = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '--exec', 'python3', '-I', '-S', '-c', probe, store.linux + '/owner.json'],
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0 and b'ORIGINAL_STORE_BIRTH_ABSENT' in result.stdout
        (root / 'store-exit-verified.json').write_text(json.dumps({'passed': True, 'proof': 'independent_proc_birth_absent'}), encoding='utf8')
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(run())
