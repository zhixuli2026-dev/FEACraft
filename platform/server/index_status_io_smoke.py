"""Exercise the public index-status route during real index and business I/O."""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

from index_search_io_smoke import (
    EDIT, FIXTURE, HELD, NEW, OLD, RUNTIME, SIZE, TIMEOUT,
    expire_content_verification, paused_request, snapshot, writer_available,
)
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


STATUS_FIXTURE = FIXTURE.replace("endswith('/search')", "endswith('/index/status')")


def exercise(api, report, baseline):
    api.create_case('Index status slow source')
    report['api_process'] = api.call('GET', '/fixture/search-process', api_prefix=False)
    path = api.prefix + '/index/status'
    assert api.call('GET', path) == {'indexed': False}
    api.put(HELD, 'engineering index status\n'.ljust(SIZE))
    api.put(EDIT, OLD)
    api.call('POST', api.prefix + '/index:rebuild', {})
    initial = api.call('GET', path)
    assert initial['indexed'] and initial['entries'] >= 2
    index = api.root / 'index' / f'case-{api.case_id}.sqlite'
    expire_content_verification(index)
    before = snapshot(index)
    assert before['text'] == OLD and before['building_uuid'] == initial['building_uuid']
    headers = {'Authorization': 'Bearer ' + api.token}
    with (httpx.Client(base_url=api.base + '/api/v1', headers=headers, timeout=50, trust_env=False) as status,
          httpx.Client(base_url=api.base + '/api/v1', headers=headers, timeout=50, trust_env=False) as writer):
        def save_while_reading(_pending, block):
            available = writer_available(api)
            reply = writer.put(api.prefix + '/file', json={'path': EDIT, 'content': NEW,
                'expected_sha256': hashlib.sha256(OLD.encode()).hexdigest()}, timeout=20)
            return {'block': block, 'writer_available': available,
                    'save_status': reply.status_code, 'save_reply': reply.json()}

        reply, evidence, trace = paused_request(api, status, path, OLD, 'edit', save_while_reading)
        evidence.update(status=reply.status_code, reply=reply.json(), trace=trace)
        report['concurrent_save'] = evidence
        assert evidence['writer_available'] is not baseline, evidence
        assert evidence['block']['deadline_active'] is not baseline, evidence
        if baseline:
            assert reply.status_code == 200 and reply.json()['building_uuid'] == initial['building_uuid']
            assert evidence['save_status'] == 409 and evidence['save_reply']['detail']['code'] == 'storage_quota_busy'
            return

        assert evidence['save_status'] == 200 and reply.status_code == 409, evidence
        assert reply.json()['detail']['code'] == 'index_source_changed'
        assert snapshot(index) == before
        fresh = api.call('GET', path)
        assert {'indexed', 'entries', 'indexed_at', 'building_uuid'} == fresh.keys()
        assert fresh['entries'] == initial['entries'] and fresh['building_uuid'] != before['building_uuid']
        assert snapshot(index)['text'] == NEW
        report['refreshed_status'] = fresh

        expire_content_verification(index)
        before_expiry = snapshot(index)

        def wait_expired(pending, block):
            assert block['deadline_active'] and 0 < block['remaining'] <= TIMEOUT, block
            time.sleep(block['remaining'] + .20)
            assert not pending.done(), 'Response completed before the real reader drained'
            return True

        reply, drained_first, trace = paused_request(api, status, path, NEW, 'expiry', wait_expired)
        report['expiry'] = {'status': reply.status_code, 'drained_first': drained_first, 'trace': trace}
        assert reply.status_code == 504 and reply.json()['detail']['code'] == 'index_status_timeout', reply.text
        assert trace['expired_at_release'] and not trace['io_started_after_expiry'], trace
        assert snapshot(index) == before_expiry
        assert api.call('GET', path)['building_uuid'] == fresh['building_uuid']

        email = uuid.uuid4().hex + '@example.com'
        viewer = api.call('POST', '/auth/register', {'email': email, 'name': 'Index status viewer',
            'password': 'isolated-test-123'})['token']
        viewer_headers = {'Authorization': 'Bearer ' + viewer}
        assert status.get(path, headers=viewer_headers).status_code == 404
        org = api.call('GET', '/org/mine')['org']['id']
        api.call('POST', f'/org/{org}/members', {'email': email, 'role': 'viewer'}, expect=201)
        api.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=viewer_headers)
        workspace = api.call('GET', '/workspaces')[0]['id']
        share_route = f'/workspaces/{workspace}/sharing'
        share = {'org_id': org, 'role': 'viewer', 'enabled': True}
        api.call('PUT', share_route, share)
        with httpx.Client(base_url=status.base_url, headers=viewer_headers, timeout=50, trust_env=False) as shared:
            permitted = shared.get(path)
            assert permitted.status_code == 200 and permitted.json()['building_uuid'] == fresh['building_uuid']
            expire_content_verification(index)

            def revoke_while_reading(_pending, _block):
                api.call('PUT', share_route, {**share, 'enabled': False})
                return True

            revoked, _, _ = paused_request(api, shared, path, NEW, 'remove', revoke_while_reading)
            assert revoked.status_code == 404 and shared.get(path).status_code == 404
        report['viewer_statuses'] = [404, permitted.status_code, revoked.status_code, 404]


def run(backend, baseline):
    from app.services import process_identity_native

    api = IsolatedServer('index-status-io-' + backend, app='index_status_fixture:app')
    evidence = api.root / 'index-status-io.json'
    report = {'passed': False, 'baseline': baseline, 'backend': backend, 'api_port': api.port,
              'real_model_calls': 0, 'mock_llm_calls': 0}
    store_context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('INDEX_STATUS_IO_EVIDENCE', api.root, flush=True)
    try:
        with store_context as store:
            config = store.settings() if store is not None else {}
            if store is not None:
                report['store_receipt'] = str(store.root / 'store.json')
            temp = api.root / 'native-temp'
            temp.mkdir()
            config.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
                          VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS=str(TIMEOUT))
            module = api.root / 'index_status_fixture.py'
            try:
                module.write_text('import os\nos.environ.update(' + repr(config) + ')\n' + STATUS_FIXTURE, encoding='utf8')
                with api:
                    exercise(api, report, baseline)
            finally:
                module.unlink(missing_ok=True)
        if store is not None:
            report['store_exit'] = store.proc.poll()
            assert report['store_exit'] == 0
        report['api_exit'] = api.proc.poll()
        assert report['api_exit'] is not None
        until = time.monotonic() + 5
        while True:
            identity = report['api_identity_after'] = process_identity_native.probe(report['api_process'])
            if identity['state'] == 'exited':
                break
            assert time.monotonic() < until, identity
            time.sleep(.05)
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1600]}
        raise
    finally:
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('INDEX_STATUS_IO_PASS', evidence, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    run('fs', args.baseline)
    if not args.baseline:
        run('s3', False)
