"""Crash disposable APIs during genuine capability probes. This exercises no model routes."""
import argparse
import asyncio
from contextlib import nullcontext
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from app.services import process_identity_native as identity
from app.services.owned_process import probe_native
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_outbox_restart_acceptance import restart
from live_llm_terminal_agent_acceptance import disposable_database
from runtime_probe_smoke import HELD, held_members, members_exited, request
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


FIXTURE = '''
import asyncio, json, os
from pathlib import Path
from fastapi import Depends
from sqlalchemy import func, select
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Case, Job, NativeRequest
from app.services import native_request_records as records, runtime_probe_execution as runtime
from app.services.owned_process import wsl_path

root = Path(__file__).parent
cut_file = root / 'probe-crash-cut.txt'
cut = cut_file.read_text().strip() if cut_file.exists() else None
assert cut in {None, 'before-registration', 'after-registration', 'after-release'}

def witness(owner, identifier, birth):
    destination = root / (cut + '-' + birth['runtime'] + '.json')
    pending = destination.with_suffix('.writing')
    pending.write_text(json.dumps({'pid': os.getpid(), 'request_id': owner.id,
        'child_id': identifier, 'birth': birth}))
    pending.replace(destination)

register = records.register
async def register_with_cut(owner, nonce, identifier, birth, distribution):
    if cut == 'before-registration':
        witness(owner, identifier, birth)
        await asyncio.Event().wait()
    await register(owner, nonce, identifier, birth, distribution)
    if cut == 'after-registration':
        witness(owner, identifier, birth)
        await asyncio.Event().wait()
records.register = register_with_cut

release = records.release
async def release_with_witness(owner, nonce, identifier, process, send):
    if cut == 'after-registration':
        (root / ('unexpected-release-' + owner.id)).touch()
        raise AssertionError('Release entered before the registration callback returned')
    await release(owner, nonce, identifier, process, send)
    if cut == 'after-release':
        witness(owner, identifier, process.record)
records.release = release_with_witness

from app.services.native_request_execution import RequestExecutor
original_command = RequestExecutor._command
def command_for_request(self, purpose, parameters):
    if self.kind not in records.GLOBAL_KINDS or cut not in {'after-registration', 'after-release'}:
        return original_command(self, purpose, parameters)
    # Command validation still uses the registered purpose and production argv.
    argv, deadline, limit = original_command(self, purpose, parameters)
    path = str if self.runtime == 'windows' else wsl_path
    argv[argv.index('--settings') - 1:] = [path(root / 'probe_hold.py'), path(root), self.owner.id,
        'registration_ack' if cut == 'after-registration' else 'owner_death',
        path(runtime.NATIVE), 'python' if self.runtime == 'windows' else 'wsl',
        json.dumps(self.profile)]
    return argv, deadline, limit
RequestExecutor._command = command_for_request

@app.get('/fixture/capability-probes')
async def snapshot(user=Depends(get_current_user), db=Depends(get_db)):
    rows = (await db.scalars(select(NativeRequest)
        .where(NativeRequest.created_by == user.id, NativeRequest.kind.in_(records.GLOBAL_KINDS))
        .order_by(NativeRequest.created_at, NativeRequest.id))).all()
    return {'pid': os.getpid(), 'lease_seconds': records.LEASE_SECONDS,
        'cases': await db.scalar(select(func.count()).select_from(Case)),
        'jobs': await db.scalar(select(func.count()).select_from(Job)),
        'probes': {row.id: {'public': records.public(row), 'db_phase': row.phase, 'version': row.version,
            'lease_expires_at': row.lease_expires_at.isoformat(), 'children_json': row.children_json,
            'heartbeat_at': row.heartbeat_at.isoformat(), 'result_json': row.result_json} for row in rows}}
'''


CUTS = {'before-registration': 'preparing', 'after-registration': 'running', 'after-release': 'running'}


def snapshot(api):
    state = api.call('GET', '/fixture/capability-probes', api_prefix=False)
    assert state['cases'] == state['jobs'] == 0
    return state


def process_states(witnesses):
    result = []
    for witness in witnesses:
        birth = witness['birth']
        host = identity.probe(birth['host'])['state']
        native = birth['native']
        native_state = None if native is None else asyncio.run(
            probe_native(native['distribution'], native['process']))['state']
        result.append((birth['runtime'], host, native_state))
    return result


def assert_exited(witnesses, members):
    def exited():
        statuses = process_states(witnesses)
        if any(host != 'exited' or native not in {None, 'exited'} for _, host, native in statuses):
            return None
        try:
            members_exited(members)
        except AssertionError:
            return None
        return statuses
    return wait_for(exited, 'actual old capability gates or members did not exit', 25)


def crash(api, cut):
    with request(api) as stream:
        paths = [api.root / (cut + '-' + runtime + '.json') for runtime in ('windows', 'wsl')]
        wait_for(lambda: all(path.is_file() for path in paths), 'both real gates missed the crash boundary', 25)
        witnesses = [json.loads(path.read_text()) for path in paths]
        state = snapshot(api)
        assert {witness['pid'] for witness in witnesses} == {state['pid']}
        assert all(host == 'match' and native in {None, 'match'} for _, host, native in process_states(witnesses))
        records = {}
        for witness in witnesses:
            row = state['probes'][witness['request_id']]['public']
            records[row['id']] = row
            assert row['case_id'] is None and row['phase'] == 'active' and row['ended_at'] is None
            assert len(row['children']['children']) == 1
            child = row['children']['children'][witness['child_id']]
            assert child['phase'] == CUTS[cut] and child['stop'] is None and child['ended_at'] is None
            assert child['identity'] == (None if cut == 'before-registration' else witness['birth'])
            if cut == 'after-registration':
                assert not (api.root / ('payload-' + row['id'] + '.json')).exists()
        members = held_members(api, list(records.values())) if cut == 'after-release' else []
        (api.root / (cut + '-pre-crash.json')).write_text(json.dumps({'witnesses': witnesses,
            'requests': records, 'members': members}, indent=2))
        kill_owned_api(api, state['pid'])
        stream.settimeout(5)
        try:
            residue = stream.recv(4096)
        except ConnectionResetError:
            residue = b''
        assert residue == b'', 'interrupted probe emitted an HTTP response'
    states = assert_exited(witnesses, members)
    if cut == 'after-registration':
        for key in records:
            assert not (api.root / ('unexpected-release-' + key)).exists()
            assert not (api.root / ('payload-' + key + '.json')).exists()
    return {'pid': state['pid'], 'phase': CUTS[cut], 'requests': records, 'states': states,
        'registration_committed': cut != 'before-registration', 'release_reached': cut == 'after-release',
        'verified_exited_descendants': len(members), 'interrupted_http_response': False}


def deployment(database_url, store, cuts):
    fixture = IsolatedServer('runtime-probe-owner-loss', app='probe_owner_loss_fixture:app', database_url=database_url)
    overrides = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n' if store else ''
    (fixture.root / 'probe_owner_loss_fixture.py').write_text(overrides + FIXTURE, encoding='utf-8')
    held = HELD.replace("if mode=='log_limit':\n", "if mode=='registration_ack': mark(os.getpid())\nif mode=='log_limit':\n")
    assert held != HELD
    (fixture.root / 'probe_hold.py').write_text(held, encoding='utf-8')
    mode = fixture.root / 'probe-crash-cut.txt'
    mode.write_text(cuts[0])
    reports = {}
    with fixture as api:
        auth = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
            'name': 'Probe owner loss acceptance', 'password': 'isolated-test-123'})
        api.token = auth['token']
        assert not snapshot(api)['probes']
        for index, cut in enumerate(cuts):
            reports[cut] = crash(api, cut)
            if index + 1 < len(cuts):
                mode.write_text(cuts[index + 1])
            else:
                mode.unlink()
            restart(api)
        old = {key: row for report in reports.values() for key, row in report['requests'].items()}
        cold = snapshot(api)
        assert len(old) == 2 * len(cuts) and set(old) == set(cold['probes'])
        assert cold['pid'] not in {report['pid'] for report in reports.values()}
        current_view = [row['public']['phase'] for row in cold['probes'].values()]
        assert cold['lease_seconds'] == 30 and 'active' in current_view

        def expired():
            state = snapshot(api)
            assert set(state['probes']) == set(old), 'startup replayed the abandoned requests'
            return state if all(row['public']['phase'] == 'recovery_required' for row in state['probes'].values()) else None
        start = time.monotonic()
        lost = wait_for(expired, 'old request leases did not expire naturally', 40)
        expires_elapsed = round(time.monotonic() - start, 3)
        for key, value in lost['probes'].items():
            start_row = cold['probes'][key]
            assert {k: v for k, v in value.items() if k != 'public'} == {k: v for k, v in start_row.items() if k != 'public'}
            public = value['public']
            assert public == {**old[key], 'phase': 'recovery_required'}
            assert value['db_phase'] == 'active' and public['cleanup_verified'] is False and public['ended_at'] is None
            assert dt.datetime.fromisoformat(value['lease_expires_at']).replace(tzinfo=dt.timezone.utc) <= dt.datetime.now(dt.timezone.utc)

        # A fresh user request may proceed and must receive different durable IDs.
        normal = api.call('GET', '/runtime/capabilities')
        assert normal['python']['reachable'] and normal['python']['packages']['cadquery']
        assert normal['wsl']['reachable'] and normal['wsl']['openfoam'] and normal['wsl']['wmake']
        assert normal['storage']['ok'] and normal['storage']['backend'] == ('s3' if store else 'fs')
        new_ids = {normal[key]['request_id'] for key in ('python', 'wsl')}
        assert len(new_ids) == 2 and not new_ids.intersection(old)
        final = snapshot(api)
        assert set(final['probes']) == set(old) | new_ids
        assert {key: final['probes'][key] for key in old} == lost['probes']
        fresh = [final['probes'][key]['public'] for key in sorted(new_ids)]
        fresh_witnesses = []
        for record in fresh:
            child = next(iter(record['children']['children'].values()))
            assert record['phase'] == 'succeeded' and record['cleanup_verified'] is True
            assert child['stop']['verified'] is True and child['phase'] == 'stopped'
            fresh_witnesses.append({'birth': child['identity']})
        fresh_states = assert_exited(fresh_witnesses, [])

    # Cold read against the original SQL store using the same scoped credential.
    reader = IsolatedServer('runtime-probe-owner-loss-reader', app='probe_owner_loss_reader:app', database_url=database_url)
    shared = {name: fixture.env[name] for name in ('VIBEFLOW_STORAGE_FS_ROOT', 'VIBEFLOW_JWT_SECRET', 'VIBEFLOW_RUNTIME_CONFIG')}
    if database_url is None:
        shared['VIBEFLOW_DATABASE_URL'] = fixture.env['VIBEFLOW_DATABASE_URL']
    script = 'import os\nos.environ.update(' + repr(shared) + ')\n' + overrides + FIXTURE
    (reader.root / 'probe_owner_loss_reader.py').write_text(script, encoding='utf-8')
    with reader as api:
        api.token = auth['token']
        assert snapshot(api)['probes'] == final['probes']
        other = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
            'name': 'Outside capability owner', 'password': 'isolated-test-123'})
        api.token = other['token']
        assert not snapshot(api)['probes']

    result = {'passed': True, 'database': 'postgresql' if database_url else 'sqlite',
        'storage': 's3' if store else 'fs', 'crashes': reports, 'natural_lease_expiry_seconds_observed': expires_elapsed,
        'old_requests_expired_without_mutation': len(old), 'fresh_requests': fresh, 'fresh_exit_states': fresh_states,
        'no_replay_on_cold_read': True, 'owner_scope_isolated': True, 'reader_root': str(reader.root),
        'fresh_watchdog_lease_seconds': cold['lease_seconds'], 'physical_hosts': 1,
        'api_exited': fixture.proc.poll() is not None and reader.proc.poll() is not None,
        'real_model_requests': 0, 'mock_llm_calls': 0}
    (fixture.root / 'runtime-probe-owner-loss.json').write_text(json.dumps(result, indent=2))
    return fixture.root


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--only-after-registration', action='store_true')
    args = parser.parse_args()
    cuts = ('after-registration',) if args.only_after_registration else tuple(CUTS)
    store = ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed',
        volume_max=6) if args.s3 else nullcontext(None)
    async with disposable_database(args.postgres) as url:
        with store as selected:
            root = await asyncio.to_thread(deployment, url, selected, cuts)
        if args.s3:
            assert selected.proc.poll() is not None
            (root / 's3-cleanup.json').write_text(json.dumps({'stopped': True, 'root': str(selected.root)}))
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    print('RUNTIME_PROBE_OWNER_LOSS_OK', json.dumps({'root': str(root), 'source_sha256': digest}), flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    asyncio.run(main())
