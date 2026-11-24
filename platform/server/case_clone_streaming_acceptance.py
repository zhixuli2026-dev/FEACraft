"""Verify version-pinned large clone streaming against actual FS and S3. No model calls."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

import httpx

from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_outbox_restart_acceptance import restart
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER

LARGE = 'results/solver-data.bin'
CHUNK = 1024 * 1024
SIZE = 48 * CHUNK
BLOCK = hashlib.sha256(b'large clone real file').digest() * (CHUNK // 32)

FIXTURE = '''
import os
from pathlib import Path
import shutil
import time
import tracemalloc
from fastapi import Depends
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import case_provision, case_source
from app.services.attempt_objects import arguments

root = Path.cwd()
source_path = 'results/solver-data.bin'
(root / 'api-birth-pid').write_text(str(os.getpid()))

def install(prefix, path, payload, *, replace=False):
    if storage._use_s3():
        with payload.open('rb') as stream:
            client = storage.s3()
            key = arguments(prefix, path)
            conditions = {'IfMatch':client.head_object(**key)['ETag']} if replace else {'IfNoneMatch':'*'}
            client.put_object(**key, Body=stream, **conditions)
    else:
        target = storage._fs_path(prefix, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if replace:
            intermediate = target.with_name('.replacement')
            shutil.copyfile(payload, intermediate)
            os.replace(intermediate, target)
        else:
            assert not target.exists()
            shutil.copyfile(payload, target)

@app.post('/fixture/clone-seed/{identifier}')
async def seed(identifier: int, db=Depends(get_db), user=Depends(get_current_user)):
    case = await get_owned_case(identifier, user, db)
    await case_provision.io(install, case.s3_prefix, source_path, root / 'solver-input.bin')
    return {'prefix':case.s3_prefix}

@app.middleware('http')
async def measure(request, call_next):
    test_limit = request.headers.get('X-Isolated-Clone-Node-Limit')
    if test_limit is not None:
        settings = get_settings()
        previous = settings.node_storage_limit_mb
        settings.node_storage_limit_mb = int(test_limit)
        try:
            return await call_next(request)
        finally:
            settings.node_storage_limit_mb = previous
    if request.headers.get('X-Isolated-Memory') != 'enabled':
        return await call_next(request)
    tracemalloc.start()
    try:
        response = await call_next(request)
        _current, peak = tracemalloc.get_traced_memory()
        (root / 'clone-memory.json').write_text(json.dumps({'application_python_peak_bytes':peak}))
        return response
    finally:
        tracemalloc.stop()

original_copy = case_source.SourceFile.copy_into
original_write = case_provision.write_file

def copy_into(source, target):
    if source.path == source_path and (root/'change-version').exists():
        (root/'change-version').unlink()
        install(source.prefix, source.path, root/'solver-input.bin', replace=True)
        (root/'live-source-replacement').touch()
    original_copy(source,target)
    if source.path == source_path and tracemalloc.is_tracing():
        claims = storage._fs_root() / '.node-storage-budget'
        sizes = [json.loads(path.read_bytes())['bytes'] for path in claims.glob('*.json')]
        if isinstance(target,Path):
            staged = target.stat().st_size
        else:
            staged = target.seek(0,2)
            target.seek(0)
        (root/'staging-bound.json').write_text(json.dumps({'staged':staged,'live_claim_bytes':max(sizes)}))

def write(plan, path, data):
    original_write(plan,path,data)
    mode = root/'clone-fault'
    if path != source_path or not mode.exists():
        return
    if mode.read_text() == 'unknown':
        install(plan['binding']['prefix'], 'Ref/unknown-source.txt', root/'unknown.bin')
    (root/'clone-in-flight.json').write_text(json.dumps(plan))
    while True:
        time.sleep(.02)

case_source.SourceFile.copy_into = copy_into
case_provision.write_file = write
'''


def clone(api, source, target, name, *, expect=200, measure=False, node_limit=None):
    headers = {'Authorization': 'Bearer ' + api.token}
    if measure:
        headers['X-Isolated-Memory'] = 'enabled'
    if node_limit is not None:
        headers['X-Isolated-Clone-Node-Limit'] = str(node_limit)
    with httpx.Client(trust_env=False, timeout=150) as client:
        answer = client.post(api.base + f'/api/v1/cases/{source}:copy', headers=headers,
                             json={'name': name, 'workspace_id': target})
        assert answer.status_code == expect, (answer.status_code, answer.text[:1000])
        return answer.json()


def assert_bytes(api, identifier, expected):
    with httpx.Client(trust_env=False, timeout=120) as client:
        with client.stream('GET', api.base + f'/api/v1/cases/{identifier}/raw?path={quote(LARGE)}',
                           headers={'Authorization': 'Bearer ' + api.token}) as response:
            assert response.status_code == 200, response.status_code
            length, digest = 0, hashlib.sha256()
            for data in response.iter_bytes(chunk_size=CHUNK):
                length += len(data)
                digest.update(data)
            assert length == SIZE and digest.hexdigest() == expected


def run_fixture(database, store):
    api = IsolatedServer('case-clone-streaming', app='clone_fixture:app', database_url=database)
    prefix = 'import os, json\n'
    if store:
        prefix += 'os.environ.update(' + repr(dict(store.settings())) + ')\n'
    (api.root / 'clone_fixture.py').write_text(prefix + FIXTURE, encoding='utf8')
    source_file = api.root / 'solver-input.bin'
    expected = hashlib.sha256()
    with source_file.open('xb') as stream:
        for _ in range(SIZE // CHUNK):
            stream.write(BLOCK)
            expected.update(BLOCK)
    expected = expected.hexdigest()
    (api.root / 'unknown.bin').write_bytes(b'preserve unexpected clone data')
    birth = api.root / 'api-birth-pid'
    crash = api.root / 'clone-in-flight.json'
    fault = api.root / 'clone-fault'

    with api:
        source = api.create_case('Large clone source')
        workspace = api.call('GET', '/workspaces')[0]['id']
        target = api.call('POST', '/workspaces', {'name': 'Large clone target'})['id']
        api.call('POST', f"/fixture/clone-seed/{source['id']}", api_prefix=False)
        api.put('Ref/小文件.txt', 'small text')
        api.put('Ref/empty.txt', '')

        constrained = clone(api, source['id'], target, 'Rejected before staging', expect=409,
                            node_limit=24 if store else 120)
        assert constrained['detail']['code'] == 'node_storage_full', constrained
        assert not api.call('GET', f'/workspaces/{target}/cases')

        started = time.monotonic()
        copied = clone(api, source['id'], target, 'Large streaming copy', measure=True)
        elapsed = time.monotonic() - started
        peak = json.loads((api.root / 'clone-memory.json').read_text())['application_python_peak_bytes']
        assert peak < SIZE // 2, peak
        staging = json.loads((api.root/'staging-bound.json').read_text())
        assert staging['staged'] == SIZE
        assert staging['live_claim_bytes'] >= (1 if store else 2) * SIZE
        assert not list((api.root/'files'/'.node-storage-budget').glob('*.json'))
        assert_bytes(api, copied['id'], expected)
        for path, data in [('Ref/小文件.txt', b'small text'), ('Ref/empty.txt', b'')]:
            assert api.call('GET', f"/cases/{copied['id']}/raw?path={quote(path)}", raw=True) == data

        workspace_copy = api.call('POST', f'/workspaces/{workspace}:copy',
                                  {'name': 'Large workspace copy', 'include_cases': True})
        workspace_cases = api.call('GET', f"/workspaces/{workspace_copy['id']}/cases")
        assert len(workspace_cases) == 1
        assert_bytes(api, workspace_cases[0]['id'], expected)

        (api.root / 'change-version').touch()
        clone(api, source['id'], target, 'Same bytes with changed version', expect=409)
        assert (api.root / 'live-source-replacement').exists()
        assert_bytes(api, source['id'], expected)
        assert all(item['name'] != 'Same bytes with changed version'
                   for item in api.call('GET', f'/workspaces/{target}/cases'))

        crash_reports = {}
        for mode in ('clean', 'unknown'):
            expected_name = 'Interrupted ' + mode
            fault.write_text(mode, encoding='utf8')
            first_pid = int(birth.read_text())
            with ThreadPoolExecutor(max_workers=1) as requests:
                request = requests.submit(clone, api, source['id'], target, expected_name)
                wait_for(lambda: crash.exists() or request.done(), 'Clone did not reach its durable file write', 90)
                if not crash.exists():
                    request.result()
                    raise AssertionError('Clone returned before its crash gate')
                plan = json.loads(crash.read_text())
                assert plan['files'][LARGE] == {'sha256': expected, 'size': SIZE}
                kill_owned_api(api, first_pid)
                try:
                    request.result(timeout=15)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The request completed during an actual API crash')
            fault.unlink()
            crash.unlink()
            restart(api)
            assert int(birth.read_text()) != first_pid
            recovered = api.call('POST', '/case-provisions:recover')['results']
            if mode == 'clean':
                assert any(item.get('id') == plan['id'] and item['status'] == 'rolled_back' for item in recovered), recovered
            else:
                assert any(plan['id'] in item.get('record','') and item['status'] == 'recovery_required'
                           for item in recovered), recovered
                target_prefix = plan['binding']['prefix']
                if store:
                    # The app verifies the logical namespace, so use a fresh read
                    # through its normal catalog after the recovery request.
                    raw = store.client.get_object(Bucket=store.bucket, Key=target_prefix+'/.vibeflow/live-store/head.json')
                    with raw['Body'] as body:
                        entry = json.loads(body.read())['files']['Ref/unknown-source.txt']
                    raw = store.client.get_object(Bucket=store.bucket, Key=entry['key'])
                    with raw['Body'] as body:
                        unknown = body.read()
                else:
                    unknown = (api.root/'files'/target_prefix/'Ref/unknown-source.txt').read_bytes()
                assert unknown == b'preserve unexpected clone data'
            retried = clone(api, source['id'], target, expected_name)
            assert_bytes(api, retried['id'], expected)
            assert sum(item['name'] == expected_name for item in api.call('GET', f'/workspaces/{target}/cases')) == 1
            crash_reports[mode] = {'plan_id': plan['id'], 'exact_retry_bytes': True,
                                   'unknown_preserved': mode == 'unknown'}

    assert api.proc.poll() is not None
    (api.root/'clone-streaming-result.json').write_text(json.dumps({
        'passed': True, 'storage': 's3' if store else 'fs', 'database': 'postgresql' if database else 'sqlite',
        'large_file_bytes': SIZE, 'sha256': expected, 'application_python_peak_bytes': peak,
        'measured_clone_seconds': round(elapsed, 3), 'source_same_bytes_replacement_rejected': True,
        'node_peak_rejected': True, 'admitted_staging': staging, 'node_claim_released': True,
        'small_unicode_and_empty_preserved': True, 'both_clone_routes': True, 'crashes': crash_reports,
        'real_model_requests': 0, 'mock_llm_calls': 0, 'api_exited': True}, indent=2), encoding='utf8')
    print('CASE_CLONE_STREAMING_OK', api.root, flush=True)


async def main(args):
    store = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        # The fixture uses 32 MiB volumes and intentionally retains immutable
        # versions across aborted copies. Allocate room for the full workflow.
        store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed', volume_max=32)
    async with disposable_database(args.postgres) as database:
        with store as active:
            await asyncio.to_thread(run_fixture, database, active)
    if args.s3:
        assert active.proc.poll() is not None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    asyncio.run(main(parser.parse_args()))
