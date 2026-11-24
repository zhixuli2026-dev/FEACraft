"""Compare bounded file restore with the original loader on a real saved WSL recovery."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from urllib.parse import urlencode
import uuid

from testsupport import IsolatedServer, SERVER


MEMBER = 'Ref/recovery-download.bin'
SIZE = 6 * 1024 * 1024
SHA = '65221fb13af7cd0566dc5022770252aa8ea67b9c258e37fc204934682856a4eb'
RECEIPT = '970975d33c36519751f9da5a02d473b4e6d08c14844c8b1f009695b46b21a5c1'

OBSERVER = r'''
import asyncio
from pathlib import Path
import os
import tracemalloc

os.environ.update(VIBEFLOW_NODE_STORAGE_LIMIT_MB='32', VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
from app.main import app
from app.services import node_storage_budget as node, sandbox_recovery_restore as restore

claim = None
previous = None

@app.middleware('http')
async def trace_restore(request, call_next):
    global previous
    if not request.url.path.endswith('/restore'):
        return await call_next(request)
    tracemalloc.start()
    try:
        return await call_next(request)
    finally:
        previous = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

@app.get('/_rb12/peak')
async def peak():
    return {'peak': previous}

@app.post('/_rb12/source-peak')
async def source_peak(body: dict):
    value, selected = await restore.recovery.load(body['sid'], body['rid'], body['user_id'])
    tracemalloc.start()
    try:
        payload = await restore.files._io(restore.payload, selected, value['manifest'], [body['path']])
        return {'peak': tracemalloc.get_traced_memory()[1], 'bytes': len(payload[body['path']])}
    finally:
        tracemalloc.stop()

@app.post('/_rb12/capacity')
async def capacity(body: dict):
    global claim
    if body['hold']:
        assert claim is None
        used = await asyncio.to_thread(node.observe)
        size = 32 * 1024 * 1024 - used['bytes'] - used['reserved_bytes'] - 4 * 1024 * 1024
        assert size > 0
        scope = node.reserve(size)
        await asyncio.to_thread(scope.__enter__)
        claim = scope
    else:
        scope, claim = claim, None
        if scope is not None:
            await asyncio.to_thread(scope.__exit__, None, None, None)
    return await asyncio.to_thread(node.observe)
'''


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def clone(source, destination):
    archived = source / 'test.sqlite'
    cache = destination / 'original-receipt'
    cache.mkdir()
    database = cache / archived.name
    shutil.copyfile(archived, database)
    archived_wal = archived.with_name(archived.name + '-wal')
    if archived_wal.is_file():
        shutil.copyfile(archived_wal, cache / archived_wal.name)
    latest = destination / 'test.sqlite'
    # SQLite may create a shared-memory sidecar in this copy, never in the original receipt.
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(latest) as copied:
            original.backup(copied)
    with sqlite3.connect(latest) as copied:
        rows = copied.execute("SELECT id,case_id,created_by,profile_json,result_json FROM native_requests "
                              "WHERE phase='succeeded' AND kind='terminal.recover'").fetchall()
        assert len(rows) == 1
        identifier, case_id, user_id, raw_profile, raw_result = rows[0]
        receipt = json.loads(raw_result)
        profile = json.loads(raw_profile)
        tree = Path(profile['root'])
        assert tree.parent == source / 'files' / '.terminal-recovery'
        assert receipt['manifest_sha256'] == sha(tree / 'manifest.json') == RECEIPT
        assert receipt['archive_sha256'] == sha(tree / 'recovery.zip')
        assert (tree / 'files' / MEMBER).stat().st_size == SIZE and sha(tree / 'files' / MEMBER) == SHA
        assert json.loads((tree / 'complete.json').read_text(encoding='utf-8')) == receipt
        prefix, = copied.execute('SELECT s3_prefix FROM cases WHERE id=?', (case_id,)).fetchone()
        assert profile['source_prefix'] == prefix
        email, = copied.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        new_root = destination / 'files'
        adopted = new_root / '.terminal-recovery' / tree.name
        shutil.copytree(source / 'files' / prefix, new_root / prefix)
        shutil.copytree(tree, adopted, ignore=shutil.ignore_patterns('node-claim.json'))
        relocated = {**profile, 'root': str(adopted),
                     'image_root': str(new_root / '.terminal-export' / Path(profile['image_root']).name)}
        copied.execute('UPDATE native_requests SET profile_json=? WHERE id=?',
                       (json.dumps(relocated), identifier))
    return identifier, case_id, user_id, profile['session_id'], email, adopted / 'files' / MEMBER


def verify(args):
    api = IsolatedServer('rb12-recovery-payload', app='observed_payload:app')
    api.root.joinpath('observed_payload.py').write_text(OBSERVER, encoding='utf-8')
    evidence = api.root / 'restore-payload.json'
    result = {'passed': False, 'baseline': args.baseline, 'model_calls': 0, 'input_sha256': SHA, 'input_bytes': SIZE}
    try:
        rid, case_id, user_id, sid, email, member = clone(args.receipt_root.resolve(strict=True), api.root.resolve())
        with api:
            credentials = api.call('POST', '/auth/login', {'email': email, 'password':'isolated-test-123'})
            api.token = credentials['token']
            endpoint = f'/terminal/sessions/{sid}/recoveries/{rid}'
            case = f'/cases/{case_id}'
            target = case + '/raw?' + urlencode({'path': MEMBER})
            api.call('GET', target, expect=404)
            baseline = api.call('POST', '/_rb12/source-peak', {'sid': sid, 'rid': rid, 'user_id':user_id, 'path': MEMBER},
                                api_prefix=False)
            result['whole_file_loader'] = baseline
            assert baseline['bytes'] == SIZE and baseline['peak'] >= SIZE
            plan = api.call('POST', endpoint + '/restore-plan', {'paths':[MEMBER]})
            assert plan['files'][0]['snapshot'] == {'size': SIZE, 'sha256': SHA}
            body = {'paths':[MEMBER], 'confirm':True, 'semantics':'file_contents_only',
                    'request_id':str(uuid.uuid4()), 'plan_sha256':plan['plan_sha256']}
            capacity = api.call('POST', '/_rb12/capacity', {'hold':True}, api_prefix=False)
            assert capacity['reserved_bytes'] > 0
            blocked = api.call('POST', endpoint + '/restore', body, expect=409)
            assert blocked['detail']['code'] == 'node_storage_full', blocked
            api.call('GET', target, expect=404)
            assert api.call('POST', '/_rb12/capacity', {'hold':False}, api_prefix=False)['reserved_bytes'] == 0
            result['capacity_block'] = blocked['detail']['code']

            with member.open('r+b') as stream:
                original = stream.read(1)
                stream.seek(0)
                stream.write(bytes([original[0] ^ 1]))
            try:
                rejection = api.call('POST', endpoint + '/restore', body, expect=409)
                assert rejection['detail']['code'] == 'recovery_evidence_invalid', rejection
                api.call('GET', target, expect=404)
            finally:
                with member.open('r+b') as stream:
                    stream.write(original)
            assert sha(member) == SHA
            result['same_size_corruption'] = rejection['detail']['code']

            applied = api.call('POST', endpoint + '/restore', body)
            assert applied['written_files'] == [MEMBER] and applied['replayed'] is False
            measured = api.call('GET', '/_rb12/peak', api_prefix=False)['peak']
            if args.baseline:
                assert measured >= SIZE, (measured, baseline)
            else:
                original_receipt = args.baseline_receipt.read_bytes()
                before = json.loads(original_receipt)
                assert before['passed'] is True and before['baseline'] is True and before['model_calls'] == 0
                assert before['input_sha256'] == before['restored_sha256'] == SHA and before['input_bytes'] == SIZE
                saved_bytes = before['restore_http_peak'] - measured
                assert measured > 0 and saved_bytes >= SIZE * 3 // 4, (measured, before['restore_http_peak'])
                result['baseline_receipt_sha256'] = hashlib.sha256(original_receipt).hexdigest()
                result['baseline_http_peak'] = before['restore_http_peak']
                result['peak_reduction_bytes'] = saved_bytes
            restored = api.call('GET', target, raw=True)
            assert hashlib.sha256(restored).hexdigest() == SHA and len(restored) == SIZE
            result['restore_http_peak'] = measured
            result['restored_sha256'] = SHA
            assert api.call('GET', endpoint + '/restores/' + body['request_id'])['file_transaction_id'] == applied['file_transaction_id']
            api.call('PUT', case + '/file', {'path': MEMBER, 'content':'later project change'})
            replay = api.call('POST', endpoint + '/restore', body)
            assert replay['replayed'] is True and replay['file_transaction_id'] == applied['file_transaction_id']
            assert api.call('GET', target, raw=True) == b'later project change'
            result['replayed_without_overwrite'] = True
            assert api.call('POST', '/_rb12/capacity', {'hold':False}, api_prefix=False)['reserved_bytes'] == 0
        result['api_exit_code'] = api.proc.poll()
        assert result['api_exit_code'] is not None
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1500]}
        raise
    finally:
        evidence.write_text(json.dumps(result,ensure_ascii=False,indent=2) + '\n',encoding='utf-8')
    print('RB12_RECOVERY_PAYLOAD_PASS', evidence, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt-root', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--baseline', action='store_true')
    mode.add_argument('--baseline-receipt', type=Path)
    verify(parser.parse_args())
