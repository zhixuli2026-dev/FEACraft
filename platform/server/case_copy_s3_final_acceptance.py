"""Authenticated S3 copy staging, SQL concurrency and cold recovery without model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

from case_copy_final_stage_acceptance import interrupt_and_restart
from case_copy_source_lock_smoke import EDIT, LARGE, RUNTIME, digest_response, read_observation, seed, writer_available
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.services import case_provision, jobrunner, node_storage_budget, process_identity_native, s3_live_store
from app.services.case_source import SourceFile
from app.services.request_deadline import current

root = Path(__file__).parent
phase = ContextVar('s3_clone_phase', default='')
copying = ContextVar('s3_clone_copying', default=False)
real_copy = SourceFile.copy_into
real_get = s3_live_store.Client.get_object
real_write = case_provision.write_file
real_witness = case_provision.record_witness
real_register = jobrunner.supervisor_register


def record(name, data):
    path = root / (name + '.json')
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(data), encoding='utf8')
    temporary.replace(path)


class ObservedBody:
    def __init__(self, body):
        self.body = body
        self.first = True

    def __getattr__(self, name):
        return getattr(self.body, name)

    def read(self, size):
        block = self.body.read(size)
        if block and self.first:
            self.first = False
            name = phase.get()
            record(name, {'first_block': len(block), 'node_reserved': node_storage_budget.observe()['reserved_bytes'],
                          'remaining': current().remaining()})
            until = time.monotonic() + 45
            while not (root / ('release-' + name)).exists():
                if time.monotonic() >= until:
                    raise TimeoutError('Actual remote body inspection was not released')
                time.sleep(.025)
        return block


def copy_into(self, destination):
    token = copying.set(phase.get() in {'case-copy', 'workspace-copy', 'deadline'} and self.path == 'Ref/archive-large.bin')
    try:
        return real_copy(self, destination)
    finally:
        copying.reset(token)


def get(self, **args):
    response = real_get(self, **args)
    if copying.get() and args['Key'].endswith('/Ref/archive-large.bin'):
        response['Body'] = ObservedBody(response['Body'])
    return response


def crash(plan, code):
    record(phase.get(), {key: plan[key] for key in ('id', 'case_id', 'workspace_id', 'binding')})
    os._exit(code)


def write_file(plan, path, data):
    real_write(plan, path, data)
    if phase.get() == 'crash-draft' and path == 'Ref/archive-large.bin':
        crash(plan, 85)


async def witness(db, case, plan):
    if phase.get() == 'crash-bound':
        crash(plan, 86)
    await real_witness(db, case, plan)


def register(kind, job_id, case_id, cancel):
    record('job-supervisor', {'kind': kind, 'case_id': case_id, 'job_id': job_id})
    return real_register(kind, job_id, case_id, cancel)


SourceFile.copy_into = copy_into
s3_live_store.Client.get_object = get
case_provision.write_file = write_file
case_provision.record_witness = witness
jobrunner.supervisor_register = register


@app.get('/fixture/s3-copy-owner')
def observed_owner(user=Depends(get_current_user)):
    return {'identity': process_identity_native.observe(os.getpid()),
            'reserved': node_storage_budget.observe()['reserved_bytes']}


class Observe:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        value = dict(scope.get('headers', ())).get(b'x-fixture-final-copy', b'')
        token = phase.set(value.decode('ascii') if scope.get('path', '').endswith(':copy') else '')
        settings = get_settings()
        previous = settings.case_copy_timeout_seconds if phase.get() == 'deadline' else None
        if previous is not None:
            settings.case_copy_timeout_seconds = 30
        try:
            return await self.app(scope, receive, send)
        finally:
            phase.reset(token)
            if previous is not None:
                settings.case_copy_timeout_seconds = previous


app = Observe(app)
'''


def exercise(api, store, report):
    from app.services import s3_live_store

    proof = '/fixture/s3-copy-owner'
    source = api.create_case('Full remote copy source')
    source_id = source['id']
    owner_token = api.token
    workspace = api.call('GET', '/workspaces')[0]['id']
    email = api.call('GET', '/auth/me')['email']
    report['api_processes'] = [api.call('GET', proof, api_prefix=False)['identity']]
    source_prefix, expected = seed(api, store)
    api.put(EDIT, 'same content on the copy')
    report['source'] = expected
    other = api.create_case('Unrelated account writable during copy')
    other_headers = {'Authorization': 'Bearer ' + api.token}
    api.token, api.prefix = owner_token, f'/cases/{source_id}'
    cases = f'/workspaces/{workspace}/cases'
    route = api.prefix + ':copy'
    headers = {'Authorization': 'Bearer ' + owner_token}
    logical = s3_live_store.Client(store.client)

    with httpx.Client(base_url=api.base + '/api/v1', headers=headers, trust_env=False, timeout=150) as client:
        def pause(label, endpoint, data, intervening):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, endpoint, json=data, headers={'X-Fixture-Final-Copy': label})
                try:
                    observed = read_observation(api, label + '.json')
                    assert 0 < observed['first_block'] <= 1048576 and observed['node_reserved'] >= expected['size']
                    assert not pending.done() and writer_available(api)
                    observed.update(intervening())
                finally:
                    (api.root / ('release-' + label)).touch()
                response = pending.result(timeout=140)
            report['checks'].append({'name': label, 'sql_writer_available': True, 'copy_http': response.status_code, **observed})
            return response

        def write_separate_account():
            row = client.put(f'/cases/{other["id"]}/file', headers=other_headers,
                             json={'path': EDIT, 'content': 'persisted while 40 MiB source copy was paused'}, timeout=30)
            assert row.status_code == 200, row.text
            same = client.put(api.prefix + '/file', json={'path': EDIT, 'content': 'must not overwrite'}, timeout=15)
            assert same.status_code == 409 and same.json()['detail']['code'] == 'storage_quota_busy', same.text
            return {'other_account_http': row.status_code, 'own_account_http': same.status_code}

        response = pause('case-copy', route, {'name': 'Remote case committed once'}, write_separate_account)
        assert response.status_code == 200, response.text
        copied = response.json()['id']
        assert digest_response(client, copied, LARGE) == expected
        assert client.get(f'/cases/{copied}/raw', params={'path': EDIT}).text == 'same content on the copy'
        job = api.call('POST', f'/cases/{copied}/jobs', {'command': 'cmd /c echo COPIED_CASE_RUN', 'runtime': 'windows'})
        done = api.wait_job(job['id'], timeout=90)
        assert done['case_id'] == copied and done['attempt']['process_stop']['verified'] is True, done
        assert read_observation(api, 'job-supervisor.json') == {'kind': 'job', 'job_id': job['id'], 'case_id': copied}
        report['checks'].append({'name': 'large-readback-and-real-job-on-copy', 'case_id': copied,
                                 'job_id': job['id'], 'exit_code': done['exit_code'], 'verified_stop': True})

        workspace_route = f'/workspaces/{workspace}:copy'
        workspace_body = {'name': 'Atomic remote workspace copy', 'include_cases': True}

        def source_write():
            logical.put_object(Bucket=store.bucket, Key=source_prefix + '/Ref/new-during-workspace-copy.txt',
                               Body=b'acknowledged concurrent source update', IfNoneMatch='*')
            return {'external_s3_write': True}

        stale = pause('workspace-copy', workspace_route, workspace_body, source_write)
        assert stale.status_code == 409 and '源工作区' in stale.text, stale.text
        assert workspace_body['name'] not in {row['name'] for row in api.call('GET', '/workspaces')}
        repeated = client.post(workspace_route, json=workspace_body)
        assert repeated.status_code == 200, repeated.text
        rows = api.call('GET', f'/workspaces/{repeated.json()["id"]}/cases')
        assert {row['name'] for row in rows} == {source['name'], response.json()['name']}, rows
        source_copy = next(row['id'] for row in rows if row['name'] == source['name'])
        assert digest_response(client, source_copy, LARGE) == expected
        added = client.get(f'/cases/{source_copy}/raw', params={'path': 'Ref/new-during-workspace-copy.txt'})
        assert added.status_code == 200 and added.text == 'acknowledged concurrent source update', added.text
        report['checks'].append({'name': 'workspace-retry-atomic', 'copy_http': 200, 'case_count': len(rows)})

    for mode, code in [('crash-draft', 85), ('crash-bound', 86)]:
        body = {'name': 'Recovered ' + mode}
        exit_proof = interrupt_and_restart(api, proof, route, body, email, mode, code)
        report['api_processes'].append(api.call('GET', proof, api_prefix=False)['identity'])
        plan = read_observation(api, mode + '.json')
        assert (plan['case_id'] is None) == (mode == 'crash-draft'), plan
        prefix = plan['binding']['prefix']
        assert logical.head_object(Bucket=store.bucket, Key=prefix + '/' + LARGE)['ContentLength'] == expected['size']
        assert body['name'] not in {row['name'] for row in api.call('GET', cases)}
        recovery = api.call('POST', '/case-provisions:recover')['results']
        assert any(row.get('id') == plan['id'] and row['status'] == 'rolled_back' for row in recovery), recovery
        assert not logical.list_objects_v2(Bucket=store.bucket, Prefix=prefix + '/')['Contents']
        retry = api.call('POST', route, body)
        assert retry['name'] == body['name']
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          trust_env=False, timeout=90) as client:
            assert digest_response(client, retry['id'], LARGE) == expected
        report['checks'].append({'name': mode, 'plan_id': plan['id'], 'recovery': 'rolled_back', 'retry': 200, **exit_proof})
    report['node_reserved_after'] = api.call('GET', proof, api_prefix=False)['reserved']
    assert report['node_reserved_after'] == 0


def check_deadline(api, store, report):
    proof = '/fixture/s3-copy-owner'
    source = api.create_case('Remote copy deadline source')
    workspace = api.call('GET', '/workspaces')[0]['id']
    report['api_processes'] = [api.call('GET', proof, api_prefix=False)['identity']]
    _, expected = seed(api, store)
    report['source'] = expected
    route = api.prefix + ':copy'
    request = {'name': 'No partial case after remote deadline'}
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                      trust_env=False, timeout=120) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, route, json=request, headers={'X-Fixture-Final-Copy': 'deadline'})
            try:
                observed = read_observation(api, 'deadline.json')
                assert 0 < observed['remaining'] < 30 and observed['node_reserved'] >= expected['size']
                time.sleep(observed['remaining'] + .15)
                assert not pending.done() and writer_available(api)
            finally:
                (api.root / 'release-deadline').touch()
            expired = pending.result(timeout=45)
        assert expired.status_code == 504 and expired.json()['detail']['code'] == 'case_copy_timeout', expired.text
        listing = f'/workspaces/{workspace}/cases'
        assert [row['id'] for row in api.call('GET', listing)] == [source['id']]
        recovery = api.call('POST', '/case-provisions:recover')['results']
        assert any(row['status'] == 'rolled_back' and row['case_id'] is None for row in recovery), recovery
        repeat = client.post(route, json=request)
        assert repeat.status_code == 200, repeat.text
        assert digest_response(client, repeat.json()['id'], LARGE) == expected
    report['node_reserved_after'] = api.call('GET', proof, api_prefix=False)['reserved']
    assert report['node_reserved_after'] == 0
    report['checks'].append({'name': 'deadline-drains-before-release', 'http': expired.status_code,
                             'rolled_back_draft': True, 'retry': repeat.status_code, **observed})


def run(deadline_only=False):
    api = IsolatedServer('case-copy-final-s3', app='remote_copy_fixture:app')
    fixture = api.root / 'remote_copy_fixture.py'
    report = {'passed': False, 'api_port': api.port, 'storage': 'authenticated S3', 'database': 'sqlite',
              'real_model_calls': 0, 'mock_llm_calls': 0, 'checks': []}
    print('CASE_COPY_FINAL_S3_EVIDENCE ' + str(api.root), flush=True)
    try:
        with ObjectStore(executable=RUNTIME, volume_max=36, authenticated=True) as store:
            report['s3_receipt'] = str(store.root / 'store.json')
            fixture.write_text('import os\nos.environ.update(' + repr(store.settings()) + ')\n' + FIXTURE, encoding='utf8')
            try:
                with api:
                    if deadline_only:
                        check_deadline(api, store, report)
                    else:
                        exercise(api, store, report)
            finally:
                fixture.unlink(missing_ok=True)
        assert store.proc.poll() is not None and api.proc.poll() is not None
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1500]}
        raise
    finally:
        (api.root / 'final-copy.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deadline-only', action='store_true')
    run(parser.parse_args().deadline_only)
