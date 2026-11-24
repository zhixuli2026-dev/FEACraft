"""Real WSL-to-S3 tree publication and recovery during a held 40 MiB transfer."""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import aiohttp

from s3testsupport import ObjectStore
from case_copy_final_stage_acceptance import interrupt_and_restart
from sandbox_normal_close_resource_smoke import RUNTIME, call, case_prefix, inspect_source, writer_available
from sandbox_retirement_checks import verify as verify_retirement
from testsupport import IsolatedServer


SIZE = 40 * 1024 * 1024
TARGET = 'Ref/remote-publication.bin'
EMPTY = 'Ref/exported-empty'
OLD = b'original before WSL export'
DIGEST = hashlib.sha256(bytes(SIZE)).hexdigest()

FIXTURE = r'''
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from app.main import app
from app.services import node_storage_budget, process_identity_native, sandbox_export_publication, s3_file_transactions
from app.services import s3_live_store, storage_operations
from app.services.case_write import _writes
from app.services.request_deadline import current, scope

base = Path(__file__).parent
copy_context = ContextVar('observed_remote_tree_copy', default=False)
temporary_bytes = ContextVar('remote_tree_temporary_bytes', default=0)
apply = s3_file_transactions._apply
temporary_copy = s3_file_transactions._temporary_copy
get_object = s3_live_store.Client.get_object
publish = sandbox_export_publication.publish


def observe_apply(case, directory, index, entry, side, expected_side, **options):
    selected = (side == 'after' and entry['path'] == 'Ref/remote-publication.bin'
                and (base/'observe-copy').is_file() and not (base/'copy-observed.json').exists())
    token = copy_context.set(selected)
    try:
        result = apply(case, directory, index, entry, side, expected_side, **options)
        if options.get('retained_source') is not None and (base/'exit-after-tree-upload').exists():
            (base/'exit-after-tree-upload').unlink()
            os._exit(87)
        return result
    finally:
        copy_context.reset(token)


@contextmanager
def observe_temporary_copy(size):
    token = temporary_bytes.set(size)
    try:
        with temporary_copy(size) as output:
            yield output
    finally:
        temporary_bytes.reset(token)


class Observed:
    def __init__(self, body, content_length):
        self.body = body
        self.content_length = content_length
        self.reported = False

    def read(self, amount=None):
        data = self.body.read(amount)
        if data and not self.reported:
            self.reported = True
            deadline = current()
            report = {'first_read': len(data), 'length': self.content_length,
                      'inside_sql': storage_operations.inside_control() or _writes.get() is not None,
                      'reserved_local_bytes': node_storage_budget.observe()['reserved_bytes'],
                      'transfer_temporary_bytes': temporary_bytes.get(),
                      'deadline_remaining': deadline.remaining() if deadline else None}
            target = base/'copy-observed.json'
            pending = target.with_suffix('.tmp')
            pending.write_text(json.dumps(report), encoding='utf-8')
            pending.replace(target)
            cutoff = time.monotonic()+45
            while not (base/'release-copy').exists():
                if time.monotonic() >= cutoff:
                    raise RuntimeError('Fixture did not release the actual object body')
                time.sleep(.025)
        return data

    def close(self):
        (base/'transfer-drained').touch()
        self.body.close()


def observe_get(self, **args):
    response = get_object(self, **args)
    if copy_context.get() and '/after/' in args['Key'] and args['Key'].endswith('/remote-publication.bin'):
        response['Body'] = Observed(response['Body'], response['ContentLength'])
    return response


async def observed_publish(*args):
    if (base/'short-publication').exists():
        async with scope(24):
            return await publish(*args)
    return await publish(*args)


s3_file_transactions._apply = observe_apply
s3_file_transactions._temporary_copy = observe_temporary_copy
s3_live_store.Client.get_object = observe_get
sandbox_export_publication.publish = observed_publish


@app.get('/fixture/tree-transfer-process')
def observed_process():
    return {'identity': process_identity_native.observe(os.getpid())}
'''


def rows(api, statement, *parameters):
    url = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(url, uri=True)) as db:
        return db.execute(statement, parameters).fetchall()


def arm(api):
    for name in ('copy-observed.json', 'release-copy', 'transfer-drained'):
        (api.root/name).unlink(missing_ok=True)
    (api.root/'observe-copy').touch()


async def other_case(api, name):
    workspace = (await call(api, 'GET', '/workspaces'))[0]['id']
    return (await call(api, 'POST', f'/workspaces/{workspace}/cases', {'name': name}))['id']


async def while_transfer(api, other, *, session_id=None, request=None, scratch_bytes=0):
    marker = api.root/'copy-observed.json'
    deadline = time.monotonic()+120
    while not marker.is_file():
        if session_id is not None:
            state = rows(api, 'SELECT phase,error FROM native_sessions WHERE id=?', session_id)[0]
            if state[0] in {'closed', 'failed'}:
                raise AssertionError(f'Terminal settled before the selected transfer: {state}')
        if request is not None and request.done():
            raise AssertionError(f'Restore settled before the selected transfer: {request.result()}')
        if time.monotonic() >= deadline:
            raise TimeoutError('The selected S3 body read was not observed')
        await asyncio.sleep(.08)
    evidence = json.loads(marker.read_bytes())
    assert evidence['length'] == SIZE and 0 < evidence['first_read'] <= 1024*1024
    assert not evidence['inside_sql'] and evidence['transfer_temporary_bytes'] == scratch_bytes
    assert await asyncio.to_thread(writer_available, api)
    pending = rows(api, 'SELECT state,journal_id FROM case_storage_operations WHERE case_id=? AND state=?',
                   api.case_id, 'active')
    assert len(pending) == 1 and pending[0][1]
    await call(api, 'PUT', f'/cases/{other}/file', {'path': 'Ref/concurrent.txt', 'content': 'parallel save'})
    fenced = await call(api, 'PUT', api.prefix+'/file', {'path': 'Ref/blocked.txt', 'content': 'wait for publication'}, expect=409)
    assert fenced['detail']['code'] == 'file_sync_busy', fenced
    return {**evidence, 'other_case_save': 200, 'same_case_fence': 409, 'journal_id': pending[0][1]}


async def finish_shell(api, sid):
    async with asyncio.timeout(110):
        while True:
            state = await call(api, 'GET', f'/terminal/sessions/{sid}')
            if state['phase'] in {'closed', 'failed'}:
                assert state['cleanup_verified'] is True and state['exit_code'] == 0, state
                return state
            await asyncio.sleep(.12)


async def close_case(api, store, *, timeout=False):
    await asyncio.to_thread(api.create_case, 'S3 WSL export ' + ('timeout' if timeout else 'normal'))
    prefix = case_prefix(api)
    other = await other_case(api, 'Other case of same owner')
    await call(api, 'PUT', api.prefix+'/file', {'path': TARGET, 'content': OLD.decode()})
    original = await call(api, 'GET', api.prefix+'/caseDict')
    arm(api)
    if timeout:
        (api.root/'short-publication').touch()
    command = (f'head -c {SIZE} /dev/zero > {TARGET}; mkdir {EMPTY}; chmod 751 {EMPTY}; '
               "printf '\\n// reserved S3 export\\n' >> 0-caseDict/caseDict; "
               f'sha256sum {TARGET}\r')
    async with aiohttp.ClientSession() as client:
        async with client.ws_connect(api.base+'/ws/terminal', params={
                'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
            ready = await ws.receive_json(timeout=110)
            assert ready['type'] == 'ready', ready
            sid = ready['session']
            await ws.send_json({'type': 'input', 'data': command})
            seen = b''
            async with asyncio.timeout(50):
                while f'{DIGEST}  {TARGET}'.encode() not in seen:
                    message = await ws.receive()
                    assert message.type == aiohttp.WSMsgType.BINARY, message
                    seen += message.data
                    assert len(seen) <= 64*1024
            await ws.send_json({'type': 'input', 'data': 'exit\r'})
            try:
                evidence = await while_transfer(api, other, session_id=sid)
                if timeout:
                    await asyncio.sleep(evidence['deadline_remaining'] + .1)
                    assert not (api.root/'transfer-drained').exists()
                else:
                    previous = rows(api, 'SELECT heartbeat_at FROM native_sessions WHERE id=?', sid)[0][0]
                    async with asyncio.timeout(12):
                        while rows(api, 'SELECT heartbeat_at FROM native_sessions WHERE id=?', sid)[0][0] == previous:
                            await asyncio.sleep(.2)
                    evidence['heartbeat_advanced_during_body'] = True
            finally:
                (api.root/'release-copy').touch()
            state = await finish_shell(api, sid)
    assert (api.root/'transfer-drained').exists()
    dictionary = await call(api, 'GET', api.prefix+'/caseDict')
    events = rows(api, 'SELECT id FROM durable_events WHERE case_id=? AND type=?',
                  api.case_id, 'terminal.export_published')
    if timeout:
        assert state['phase'] == 'failed' and state['error'] in {'TimeoutError', 'HTTPException'} and not events, state
        assert 'export' not in state['process']
        assert dictionary['dict_revision'] == original['dict_revision']
        assert await call(api, 'GET', api.prefix+'/raw?path='+TARGET, raw=True) == OLD
        (api.root/'short-publication').unlink()
    else:
        assert state['phase'] == 'closed' and len(events) == 1, state
        assert dictionary['dict_revision'] == original['dict_revision']+1
        assert 'reserved S3 export' in dictionary['content'] and state['process']['export']['event_id'] == events[0][0]
        assert any(row['path'] == EMPTY for row in (await call(api, 'GET', api.prefix+'/tree'))['entries'])
        observed = await asyncio.to_thread(inspect_source, store, prefix+'/'+TARGET)
        assert observed['size'] == SIZE and observed['sha256'] == DIGEST
    assert not rows(api, 'SELECT id FROM case_storage_operations WHERE case_id=? AND state!=?', api.case_id, 'settled')
    assert not rows(api, 'SELECT id FROM storage_write_reservations WHERE case_id=? AND state=?', api.case_id, 'active')
    evidence.update(session_id=sid, terminal_status=state['phase'], verified_stop=True,
                    rolled_back=timeout, transfer_drained=True)
    return evidence


async def restore_case(store, report):
    directory = None

    async def before(api):
        nonlocal directory
        directory = api.root
        await call(api, 'PUT', api.prefix+'/file', {'path': TARGET, 'content': OLD.decode()})

    async def after(api, sid, rid, proof):
        other = await other_case(api, 'Restore concurrent case')
        base = f'/terminal/sessions/{sid}/recoveries/{rid}'
        selection = {'paths': [TARGET, EMPTY], 'semantics': 'sandbox_tree_changes'}
        plan = await call(api, 'POST', base+'/restore-plan', selection)
        request_id = str(uuid.uuid4())
        body = {**selection, 'request_id': request_id, 'confirm': True, 'plan_sha256': plan['plan_sha256']}
        email = (await call(api, 'GET', '/auth/me'))['email']
        local_root = Path(json.loads(rows(api, 'SELECT profile_json FROM native_requests WHERE id=?', rid)[0][0])['root'])
        retained = local_root/'files'/TARGET
        saved = retained.with_suffix('.fixture-retained')
        assert retained.is_relative_to(api.root/'files') and retained.stat().st_size == SIZE
        (api.root/'exit-after-tree-upload').touch()
        exited = await asyncio.to_thread(interrupt_and_restart, api, '/fixture/tree-transfer-process',
                                          base+'/restore', body, email, 'after-tree-upload', 87)
        published = await asyncio.to_thread(inspect_source, store, case_prefix(api)+'/'+TARGET)
        assert published['size'] == SIZE and published['sha256'] == DIGEST
        retained.rename(saved)
        try:
            recovered = await call(api, 'POST', api.prefix+'/file-transactions:recover')
            assert recovered['result']['decision'] == 'rolled_back', recovered
            assert await call(api, 'GET', api.prefix+'/raw?path='+TARGET, raw=True) == OLD
            await call(api, 'GET', base+'/restores/'+request_id, expect=404)
        finally:
            saved.rename(retained)
        report['cold_rollback'] = {**exited, 'decision': 'rolled_back', 'local_source_unavailable': True}
        arm(api)
        restore = asyncio.create_task(call(api, 'POST', base+'/restore', body))
        try:
            report['restore'] = await while_transfer(api, other, request=restore)
        finally:
            (api.root/'release-copy').touch()
        receipt = await restore
        repeated = await call(api, 'POST', base+'/restore', body)
        assert receipt['file_transaction_id'] and not receipt['replayed']
        assert repeated['replayed'] and repeated['file_transaction_id'] == receipt['file_transaction_id']
        assert (await call(api, 'GET', base+'/restores/'+request_id))['request_id'] == request_id
        assert any(row['path'] == EMPTY for row in (await call(api, 'GET', api.prefix+'/tree'))['entries'])
        observed = await asyncio.to_thread(inspect_source, store, case_prefix(api)+'/'+TARGET)
        assert observed['size'] == SIZE and observed['sha256'] == DIGEST
        report['restore'].update(receipt=receipt, verified_sha256=DIGEST, replayed=True, api_root=str(api.root))
        return 0

    try:
        await verify_retirement(store, fixture_prefix=FIXTURE, before_sandbox=before, after_retirement=after,
            sandbox_command=f': > {TARGET}; truncate -s {SIZE} {TARGET}; mkdir {EMPTY}; chmod 751 {EMPTY}; ')
    finally:
        if directory is not None:
            (directory/'retirement_fixture.py').unlink(missing_ok=True)


def run(path, restore, timeout_only):
    api = IsolatedServer('sandbox-reserved-tree', app='sandbox_reserved_tree_fixture:app')
    report = {'passed': False, 'actual_model_calls': 0, 'mock_llm_calls': 0, 'api_root': str(api.root)}
    fixture = api.root/'sandbox_reserved_tree_fixture.py'
    print('SANDBOX_RESERVED_TREE', api.root, flush=True)
    try:
        with ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) as store:
            fixture.write_text('import os\nos.environ.update('+repr(store.settings())+')\nfrom app.main import app\n'+FIXTURE,
                               encoding='utf-8')
            if restore:
                asyncio.run(restore_case(store, report))
            else:
                with api:
                    if not timeout_only:
                        report['normal'] = asyncio.run(close_case(api, store))
                        print('RESERVED_TREE_NORMAL_PASS', flush=True)
                    report['timeout'] = asyncio.run(close_case(api, store, timeout=True))
        report['passed'] = True
    finally:
        fixture.unlink(missing_ok=True)
        path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('RESERVED_TREE_PASS', path, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--restore', action='store_true')
    modes.add_argument('--timeout-only', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    run(arguments.report, arguments.restore, arguments.timeout_only)
