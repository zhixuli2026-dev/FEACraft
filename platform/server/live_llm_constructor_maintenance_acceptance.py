"""Audit real constructor rollback, maintenance cursor restart and same-name retry."""

import asyncio
import http.client
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx


FIXTURE = '''
import json
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app import storage
from app.services import case_provision as provision, storage_maintenance as maintenance

maintenance.PROVISION_PAGE_SIZE = 2
(root/'constructor-pid').write_text(str(os.getpid()))
original_scope = provision.scope
original_load = provision.load
original_write = provision.write_file
original_sync = provision.journal._sync
active_target = ContextVar('active_constructor_target', default=None)

def wait_for_crash(plan, path):
    (root/'constructor-crash.json').write_text(json.dumps({'plan':plan,'path':path}))
    while True:
        time.sleep(.02)

@asynccontextmanager
async def constrained(seconds):
    async with original_scope(4 if (root/'slow-constructor-recovery').exists() else seconds) as deadline:
        yield deadline

def load(user_id,path):
    if (root/'slow-constructor-recovery').exists():
        (root/'constructor-scan-entered').touch()
        time.sleep(provision.current().remaining() + .2)
    return original_load(user_id,path)

def sync(directory):
    original_sync(directory)
    selected = active_target.get()
    if selected and directory == (storage._fs_path(selected[0]['binding']['prefix'], selected[1])).parent:
        wait_for_crash(*selected)

def write(plan,path,data):
    mode_path = root/'constructor-fault'
    mode = mode_path.read_text() if mode_path.exists() else ''
    target = (plan,path) if mode == 'crash' and not storage._use_s3() else None
    token = active_target.set(target)
    try:
        original_write(plan,path,data)
        if mode == 'crash':
            wait_for_crash(plan,path)
        if mode == 'rollback':
            (root/'constructor-rollback.json').write_text(json.dumps({'plan':plan,'path':path}))
            raise RuntimeError('isolate actual partial constructor rollback')
    finally:
        active_target.reset(token)

provision.scope = constrained
provision.load = load
provision.write_file = write
provision.journal._sync = sync
'''


def scenario(api, *, store=None, **_):
    from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
    from live_llm_job_repeat_acceptance import write
    from live_llm_outbox_restart_acceptance import restart

    owner = api.call('GET', '/auth/me')['id']
    workspace = api.call('GET', '/workspaces')[0]['id']
    cases = f'/workspaces/{workspace}/cases'
    prefix = f'workspaces/{owner}/.case-provisions'
    files = api.root / 'files'
    fault = api.root / 'constructor-fault'
    crash = api.root / 'constructor-crash.json'
    pid = api.root / 'constructor-pid'

    def put(storage_prefix, path, data):
        key = f'{storage_prefix}/{path}'
        if store:
            store.client.put_object(Bucket=store.bucket, Key=key, Body=data)
        else:
            target = files / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def get(storage_prefix, path):
        key = f'{storage_prefix}/{path}'
        if store:
            from botocore.exceptions import ClientError
            try:
                response = store.client.get_object(Bucket=store.bucket, Key=key)
            except ClientError as error:
                if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
                    return None
                raise
            with response['Body'] as stream:
                return stream.read()
        target = files / key
        return target.read_bytes() if target.exists() else None

    first_pid = int(pid.read_text())
    fault.write_text('crash')
    with ThreadPoolExecutor(max_workers=1) as requests:
        creation = requests.submit(api.call, 'POST', cases, {'name': 'Constructor restart target'})
        wait_for(crash.exists, 'Constructor did not stop after installing its first file', 30)
        orphan = json.loads(crash.read_text())
        orphan_plan = orphan['plan']
        target_prefix = orphan_plan['binding']['prefix']
        real_path = orphan['path']
        assert get(target_prefix, real_path) is not None
        hardlink_before = None
        if store is None:
            installed = files / target_prefix / real_path
            hardlink_before = installed.stat().st_nlink
            assert hardlink_before == 2
        kill_owned_api(api, first_pid)
        try:
            creation.result(timeout=10)
        except (httpx.TransportError, http.client.HTTPException, OSError, AssertionError):
            pass
        else:
            raise AssertionError('Constructor unexpectedly survived its own API exit')
    fault.unlink()
    restart(api)
    second_pid = int(pid.read_text())
    assert second_pid != first_pid
    unknown = {f'pending/{uuid.UUID(int=index)}.json': f'unknown:{index}'.encode() for index in range(1, 5)}
    for path, content in unknown.items():
        put(prefix, path, content)
    orphan_path = f"pending/{orphan_plan['id']}.json"
    raw_plan = get(prefix, orphan_path)
    assert raw_plan is not None

    first = api.call('POST','/case-provisions:maintenance')
    initial_cursor = first['provision_scan']
    assert initial_cursor == {'phase':'pending','next_after':str(uuid.UUID(int=2))+'.json'}, first
    assert len(first['creation']) == 2 and all(row['status']=='recovery_required' for row in first['creation']), first
    if store is None:
        assert (files / target_prefix / real_path).stat().st_nlink == 1
    assert get(prefix,orphan_path) == raw_plan
    state = get(prefix,'.maintenance.json')
    assert json.loads(state)['after'] == initial_cursor['next_after']

    # A normal constructor still has to check all existing intent, even when
    # the first maintenance page holds a cursor far before the true rollback.
    # On deadline it rejects before allocating any new case or physical prefix.
    (api.root/'slow-constructor-recovery').touch()
    rejected = api.call('POST', cases, {'name':'Constructor restart target'}, expect=409)
    assert rejected['detail']['code'] == 'case_provision_recovery_required', rejected
    assert (api.root/'constructor-scan-entered').is_file()
    (api.root/'slow-constructor-recovery').unlink()
    assert get(prefix, orphan_path) == raw_plan and get(prefix, '.maintenance.json') == state
    assert all(row['name'] != 'Constructor restart target' for row in api.call('GET', cases))

    kill_owned_api(api, second_pid)
    restart(api)
    assert int(pid.read_text()) != second_pid
    second = api.call('POST','/case-provisions:maintenance')
    assert second['provision_scan'] == {'phase':'pending','next_after':str(uuid.UUID(int=4))+'.json'}, second
    assert len(second['creation']) == 2 and {row['record'] for row in second['creation']} == set(list(unknown)[2:]), second
    assert get(prefix,orphan_path) == raw_plan
    final = api.call('POST','/case-provisions:maintenance')
    assert final['provision_scan'] is None and final['creation'] == [{
        'id':orphan_plan['id'],'case_id':orphan_plan['case_id'],'status':'rolled_back'}], final
    assert get(prefix,orphan_path) is None and get(target_prefix,real_path) is None
    receipt = json.loads(get(prefix,orphan_path.replace('pending/','history/')))
    assert receipt['outcome'] == 'rolled_back' and receipt['plan'] == orphan_plan
    assert all(get(prefix,path) == content for path,content in unknown.items())
    if store is None:
        assert not any((files / prefix / 'staging').glob('*.part'))

    same = write(api,'POST',cases,{'name':'Constructor restart target'})
    assert api.call('GET',f"/cases/{same['id']}")['name'] == 'Constructor restart target'
    assert sum(row['name']=='Constructor restart target' for row in api.call('GET',cases)) == 1

    fault.write_text('rollback')
    api.call('POST',cases,{'name':'Constructor direct recovery'},expect=500,raw=True)
    fault.unlink()
    direct_orphan = json.loads((api.root/'constructor-rollback.json').read_text())
    direct_plan = direct_orphan['plan']
    direct_pending = f"pending/{direct_plan['id']}.json"
    assert get(prefix,direct_pending) is not None
    direct = write(api,'POST',cases,{'name':'Constructor direct recovery'})
    assert sum(row['name']=='Constructor direct recovery' for row in api.call('GET',cases)) == 1
    assert get(prefix,direct_pending) is None
    assert get(direct_plan['binding']['prefix'],direct_orphan['path']) is None
    assert json.loads(get(prefix,direct_pending.replace('pending/','history/')))['outcome'] == 'rolled_back'
    assert all(get(prefix,path) == content for path,content in unknown.items())

    report = {'passed':True,'known_pending_rolled_back':2,'unknown_pending_preserved':len(unknown),
              'api_crash_and_restart_count':2,'fs_installation_hardlink_before':hardlink_before,
              'maintenance_cursor_resumed':True,'same_name_create_retried_once':same['id'],
              'direct_create_resolved_relevant_pending':direct['id'],'preallocation_deadline_observed':True,
              'staged_alias_removed_before_pending':store is None}
    (api.root/'constructor-maintenance.json').write_text(json.dumps(report,indent=2))
    return report


def history_infrastructure():
    """Exercise opt-in, real historical cleanup without calling a model."""
    from testsupport import IsolatedServer

    api = IsolatedServer('constructor-history', app='history_fixture:app')
    fixture = """from pathlib import Path
from app.main import app
from app.config import get_settings
from app.services import storage_maintenance as maintenance
get_settings().provision_history_retention_days = 1
maintenance.PROVISION_PAGE_SIZE = 2
"""
    (api.root/'history_fixture.py').write_text(fixture, encoding='utf8')
    print('CONSTRUCTOR_HISTORY_EVIDENCE', api.root, flush=True)
    with api:
        first = api.create_case('Retained original constructor case')
        owner = api.call('GET','/auth/me')['id']
        workspace = api.call('GET','/workspaces')[0]['id']
        cases = f'/workspaces/{workspace}/cases'
        originals = [first['id']]
        for number in range(3):
            originals.append(api.call('POST',cases,{'name':f'Retained historical case {number}'})['id'])
        api.call('POST','/case-provisions:recover')
        history = api.root/'files'/'workspaces'/str(owner)/'.case-provisions'/'history'
        known = list(history.glob('*.json'))
        assert len(known) == 4
        unknown = history/(str(uuid.UUID(int=1))+'.json')
        unknown.write_bytes(b'unrecognized historical storage retained')
        old = time.time() - 2 * 86400
        for record in [unknown, *known]:
            os.utime(record,(old,old))
        observed = []
        for page in range(3):
            result = api.call('POST','/case-provisions:maintenance')
            observed.append(result)
            if page < 2:
                assert result['provision_scan']['phase'] == 'history'
            else:
                assert result['provision_scan'] is None
        assert not any(record.exists() for record in known)
        assert unknown.read_bytes() == b'unrecognized historical storage retained'
        assert all(api.call('GET',f'/cases/{cid}')['id'] == cid for cid in originals)
        outcomes = [item['status'] for page in observed for item in page['history']]
        assert outcomes.count('expired_history_removed') == 4 and outcomes.count('recovery_required') == 1
    assert api.proc.poll() is not None
    (api.root/'history-maintenance.json').write_text(json.dumps({
        'passed':True,'expired_receipts':4,'original_cases_readable':4,'unknown_history_preserved':True,
        'cursor_pages':3,'real_model_requests':0,'mock_llm_calls':0,'api_exited':True},indent=2))


if __name__ == '__main__':
    if sys.argv[1:] == ['--history-infrastructure-only']:
        history_infrastructure()
    else:
        from live_llm_job_repeat_acceptance import main

        asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=8))
