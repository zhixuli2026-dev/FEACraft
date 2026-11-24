"""Actual workspace copy with concurrent source edits over FS and authenticated S3."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

import httpx

from case_copy_source_lock_smoke import FIXTURE, EDIT, LARGE, RUNTIME, digest_response, read_observation, seed, writer_available
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


def contested_copy(api, client, endpoint, body, operation):
    for name in ('first-file-block.json', 'copy-trace.json', 'release-source-read'):
        (api.root / name).unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.post, endpoint, json=body, headers={'X-Fixture-Observe-Copy':'1'})
        try:
            first = read_observation(api, 'first-file-block.json')
            assert 0 < first['source_bytes'] <= 1024 * 1024 and not pending.done(), first
            writer = writer_available(api)
            intervening = operation()
        finally:
            (api.root / 'release-source-read').touch()
            result = pending.result(timeout=150)
    trace = read_observation(api, 'copy-trace.json')
    return {'first_block': first, 'writer_available': writer, 'intervening': intervening,
            'copy': {'status':result.status_code, 'document':result.json()}, 'trace':trace}


def verify_clone(api, client, workspace, source_rows, expected_file, expected_text):
    copied = api.call('GET', f'/workspaces/{workspace}/cases')
    by_name = {row['name']:row['id'] for row in copied}
    assert len(by_name) == len(copied) and set(by_name) == {row['name'] for row in source_rows}, copied
    source_id = by_name['Primary copy source']
    text = client.get(f'/cases/{source_id}/raw', params={'path':EDIT})
    assert text.status_code == 200 and text.text == expected_text, text.text
    other = client.get(f'/cases/{by_name["Second copy source"]}/raw', params={'path':'Ref/second.txt'})
    assert other.status_code == 200 and other.text == 'Second existing member', other.text
    if 'Late added source' in by_name:
        late = client.get(f'/cases/{by_name["Late added source"]}/raw', params={'path':'Ref/late.txt'})
        assert late.status_code == 200 and late.text == 'Case added while the source was read', late.text
    digest = digest_response(client, source_id, LARGE)
    assert digest == expected_file, digest
    return {'copied_cases':by_name, 'large_file':digest, 'edited_text':text.text}


def exercise(api, store, report, baseline):
    primary = api.create_case('Primary copy source')
    workspace = api.call('GET', '/workspaces')[0]['id']
    owner = api.call('GET', '/fixture/case-copy-owner', api_prefix=False)
    report['api_process'] = owner['identity']
    _, large = seed(api, store)
    report['source_file'] = large
    initial, updated = 'initial editable source', 'revised editable source'
    api.put(EDIT, initial)
    second = api.call('POST', f'/workspaces/{workspace}/cases', {'name':'Second copy source'})
    api.call('PUT', f'/cases/{second["id"]}/file', {'path':'Ref/second.txt', 'content':'Second existing member'})
    endpoint, listing = f'/workspaces/{workspace}:copy', f'/workspaces/{workspace}/cases'
    before = api.call('GET', listing)
    assert {row['id'] for row in before} == {primary['id'],second['id']}, before
    body = {'name':'Copied workspace from real source', 'include_cases':True}
    change = {'path':EDIT, 'content':updated, 'expected_sha256':hashlib.sha256(initial.encode()).hexdigest()}
    headers = {'Authorization':'Bearer ' + api.token}
    with httpx.Client(base_url=api.base + '/api/v1', headers=headers, trust_env=False, timeout=150) as client:
        def save():
            response = client.put(api.prefix + '/file', json=change, timeout=15)
            return {'status':response.status_code, 'document':response.json()}

        edit = report['concurrent_edit'] = contested_copy(api, client, endpoint, body, save)
        assert edit['writer_available'] is not baseline, edit
        assert edit['trace']['source_bytes'] == large['size'] and edit['trace']['max_block'] <= 1024*1024, edit
        if baseline:
            assert edit['intervening']['status'] == 409 and edit['intervening']['document']['detail']['code'] == 'storage_quota_busy', edit
            assert edit['copy']['status'] == 200 and edit['trace']['constructed_targets'] == 2, edit
            copy_id = edit['copy']['document']['id']
            assert save()['status'] == 200
            expected_text = initial
        else:
            assert edit['intervening']['status'] == 200 and edit['copy']['status'] == 409, edit
            assert '源工作区已变化' in edit['copy']['document']['detail'], edit
            assert edit['trace']['constructed_targets'] == 0 and not edit['first_block']['source_owner_grant'], edit
            assert all(row['name'] != body['name'] for row in api.call('GET', '/workspaces'))

            def add_case():
                return api.call('POST', f'/workspaces/{workspace}/cases', {'name':'Late added source'})

            membership = report['concurrent_member'] = contested_copy(api, client, endpoint, body, add_case)
            assert membership['writer_available'] and membership['trace']['constructed_targets'] == 0, membership
            assert not membership['first_block']['source_owner_grant'], membership
            assert membership['copy']['status'] == 409 and '源工作区已变化' in membership['copy']['document']['detail'], membership
            assert all(row['name'] != body['name'] for row in api.call('GET', '/workspaces'))
            before = api.call('GET', listing)
            added_case = membership['intervening']['id']
            assert {row['id'] for row in before} == {primary['id'],second['id'],added_case}, before
            api.call('PUT', f'/cases/{added_case}/file', {
                'path':'Ref/late.txt', 'content':'Case added while the source was read'})
            retried = client.post(endpoint, json=body)
            report['retry'] = {'status':retried.status_code, 'document':retried.json()}
            assert retried.status_code == 200, retried.text
            copy_id = retried.json()['id']
            expected_text = updated

            (api.root / 'copy-trace.json').unlink()
            empty = client.post(endpoint, json={'name':'Copied workspace only', 'include_cases':False},
                                headers={'X-Fixture-Observe-Copy':'1'})
            assert empty.status_code == 200, empty.text
            assert api.call('GET', f'/workspaces/{empty.json()["id"]}/cases') == []
            empty_trace = read_observation(api, 'copy-trace.json')
            assert empty_trace['blocks'] == 0 and empty_trace['constructed_targets'] == 0, empty_trace
            report['workspace_only'] = empty_trace
            outsider = api.call('POST', '/auth/register', {'email':uuid.uuid4().hex + '@example.com',
                'name':'Different workspace owner', 'password':'isolated-test-123'})
            foreign = client.post(endpoint, json={'name':'Unauthorized workspace copy', 'include_cases':True},
                                  headers={'Authorization':'Bearer ' + outsider['token']})
            report['foreign_copy_status'] = foreign.status_code
            assert foreign.status_code == 404, foreign.text
        workspaces = api.call('GET', '/workspaces')
        assert [row['id'] for row in workspaces if row['name'] == body['name']] == [copy_id], workspaces
        report['readback'] = verify_clone(api, client, copy_id, before, large, expected_text)
        current = client.get(api.prefix + '/raw', params={'path':EDIT})
        assert current.status_code == 200 and current.text == updated, current.text
    report['node_reserved_after'] = api.call('GET', '/fixture/case-copy-owner', api_prefix=False)['reserved_bytes']
    assert report['node_reserved_after'] == 0


def run(backend, baseline=False):
    from app.services import process_identity_native

    api = IsolatedServer('workspace-copy-source-' + backend, app='clone_observation:app')
    target = api.root / 'workspace-copy-source-lock.json'
    report = {'passed':False, 'backend':backend, 'baseline':baseline, 'api_port':api.port,
              'real_model_calls':0, 'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('WORKSPACE_COPY_SOURCE ' + str(api.root), flush=True)
    try:
        with context as store:
            configuration = store.settings() if store else {}
            if store is not None:
                report['s3_receipt'] = str(store.root / 'store.json')
            app_fixture = api.root / 'clone_observation.py'
            scratch = api.root / 'native-temp'
            scratch.mkdir()
            configuration.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
            try:
                app_fixture.write_text('import os\nos.environ.update(' + repr(configuration) + ')\n' + FIXTURE, encoding='utf8')
                with api:
                    exercise(api, store, report, baseline)
            finally:
                app_fixture.unlink(missing_ok=True)
        if store is not None:
            report['s3_wait_returncode'] = store.proc.poll()
            assert report['s3_wait_returncode'] is not None
        report['api_wait_returncode'] = api.proc.poll()
        assert report['api_wait_returncode'] is not None
        api.proc = None
        until = time.monotonic() + 5
        while True:
            report['api_exit'] = process_identity_native.probe(report['api_process'])
            try:
                with socket.create_connection(('127.0.0.1', api.port), timeout=.2):
                    report['api_port_closed'] = False
            except ConnectionRefusedError:
                report['api_port_closed'] = True
            except TimeoutError:
                report['api_port_closed'] = None
            stopped = report['api_exit']['state'] == 'exited' and report['api_port_closed'] is True
            if stopped or time.monotonic() >= until:
                break
            time.sleep(.05)
        report['api_os_observation_pending'] = not stopped
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1400]}
        raise
    finally:
        target.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print('WORKSPACE_COPY_SOURCE_OK ' + str(target), flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    baseline = os.environ.get('VIBEFLOW_WORKSPACE_COPY_BASELINE') == '1'
    run('fs', baseline=baseline)
    if not baseline:
        run('s3')
