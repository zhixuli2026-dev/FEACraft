"""Actual API/WSL ownership inspection and isolated SQL corruption checks.

No model/provider/CLI inference is simulated or invoked. The terminal runs a real
shell; protocol faults modify only the disposable database, never process probes.
"""
import asyncio
import json
import os
import subprocess
import uuid

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from testsupport import IsolatedServer, SERVER
from app.models import NativeRequest, NativeSession
from app.services import process_identity_native as identity


async def verify(store=None, database_url=None, mode='owner-death'):
    api = IsolatedServer('sandbox-recovery-inspection', app='inspection_fixture:app', database_url=database_url)
    (api.root / 'inspection_fixture.py').write_text(
        'import os\nos.environ.update(' + repr(store.settings() if store else {}) + ')\nfrom app.main import app\n',
        encoding='utf8')
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    report = {'passed': False, 'storage': 's3' if store else 'fs', 'mode': mode,
              'database': 'postgresql' if database_url else 'sqlite', 'llm_calls': 0,
              'mock_llm_calls': 0, 'checks': [], 'recovery_performed': False}

    def record(name, kind='actual_infrastructure', **evidence):
        report['checks'].append({'name': name, 'kind': kind, **evidence})
        (api.root / 'checks.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        print('RECOVERY_INSPECTION_PASS ' + name, flush=True)

    async def call(method, path, **kwargs):
        return await asyncio.to_thread(api.call, method, path, **kwargs)

    engine = create_async_engine(database_url or 'sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
    try:
        with api:
            api.create_case('Recovery ownership evidence')
            original_token = api.token
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(api.base + '/ws/terminal', params={
                        'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                    ready = await ws.receive_json(timeout=90)
                    assert ready['type'] == 'ready', ready
                    sid = ready['session']
                    endpoint = '/terminal/sessions/' + sid + '/recovery-inspection'
                    live = await call('GET', endpoint)
                    assert live['status'] == 'blocked' and live['reason'] in {
                        'owner_still_active', 'records_changed_during_inspection'}, live
                    record('live_owner_refused', evidence=live)
                    suffix = uuid.uuid4().hex
                    proof = 'REAL_INSPECTION_' + suffix
                    # The complete marker is absent from the input, so terminal
                    # echo cannot satisfy this assertion on either LF or CRLF.
                    extra = "mkdir Ref/recovery-empty; chmod 600 Ref/inspection.txt; " if mode == 'recover' else ''
                    await ws.send_json({'type': 'input', 'data': "printf '%s%s\\n' 'REAL_INSPECTION_' '" + suffix + "' > Ref/inspection.txt; " + extra + "cat Ref/inspection.txt\r"})
                    output = b''
                    async with asyncio.timeout(30):
                        while proof.encode() not in output:
                            message = await ws.receive()
                            assert message.type == aiohttp.WSMsgType.BINARY, message
                            output += message.data
                            assert len(output) <= 1024 * 1024
                            (api.root / 'terminal-output.bin').write_bytes(output)
                    record('real_shell_wrote_and_read_file')
                    await call('GET', endpoint, expect=401, extra_headers={'Authorization': ''})
                    stranger = await call('POST', '/auth/register', body={
                        'email': uuid.uuid4().hex + '@example.com', 'password': 'isolated-test-123', 'name': 'Other owner'})
                    await call('GET', endpoint, expect=404, extra_headers={'Authorization': 'Bearer ' + stranger['token']})
                    await call('GET', '/terminal/sessions/' + str(uuid.uuid4()) + '/recovery-inspection', expect=404)
                    record('anonymous_other_user_and_unknown_session_refused')
                    if mode == 'recover':
                        recovery_endpoint = '/terminal/sessions/' + sid + '/recoveries'
                        live_recovery = await call('POST', recovery_endpoint, expect=409)
                        assert live_recovery['detail']['code'] in {'owner_still_active', 'records_changed_during_inspection'}
                        await call('POST', recovery_endpoint, expect=401, extra_headers={'Authorization': ''})
                        await call('POST', recovery_endpoint, expect=404, extra_headers={'Authorization': 'Bearer ' + stranger['token']})
                        async with engine.connect() as db:
                            launched = list(await db.scalars(select(NativeRequest.id).where(NativeRequest.kind == 'terminal.recover')))
                        assert not launched
                        record('live_or_unauthorized_recovery_rejected_before_native_registration')
                    if mode == 'closed':
                        await call('DELETE', '/terminal/sessions/' + sid)
                        closed = await call('GET', endpoint)
                        assert closed['status'] == 'processes_exited', closed
                        assert any(c['role'] == 'terminal_owner' and c['state'] == 'match' for c in closed['checks'])
                        assert all(c['state'] == 'exited' for c in closed['checks'] if 'owner' not in c['role'])
                        record('completed_session_with_live_api_has_no_launch_authority', evidence=closed)
                        report['passed'] = True
                        return
                    api_birth = identity.observe(api.proc.pid)
                    original_args = api.proc.args
                    api.proc.kill()
                    await asyncio.to_thread(api.proc.wait, 10)
                    assert identity.probe(api_birth)['state'] == 'exited'
                    record('actual_api_killed_without_terminal_close')

            # Restart only after the exact previous handle is terminal, retaining
            # the same isolated DB/files/JWT, without replaying any shell input.
            api.proc = subprocess.Popen(original_args, cwd=api.root, env=api.env, stdout=api.log,
                                        stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            async with asyncio.timeout(35):
                while True:
                    assert api.proc.poll() is None, 'Restarted isolated API exited; inspect api.log'
                    try:
                        if (await call('GET', '/api/health', api_prefix=False)).get('ok'):
                            break
                    except OSError:
                        pass
                    await asyncio.sleep(.1)
            api.token = original_token
            async with asyncio.timeout(60):
                while True:
                    observed = await call('GET', endpoint)
                    if observed['status'] == 'processes_exited':
                        break
                    assert observed['reason'] in {'process_still_active', 'process_exit_unverified', 'inspection_timeout'}, observed
                    await asyncio.sleep(.2)
            assert not observed['image_verified'] and not observed['recovery_performed'] and not observed['command_replayed']
            assert all(check['state'] == 'exited' for check in observed['checks'])
            assert any(check['role'].startswith('export:') for check in observed['checks'])
            record('restarted_api_observed_all_original_process_births_exited', evidence=observed)

            async with engine.connect() as db:
                session = (await db.execute(select(NativeSession.__table__).where(NativeSession.id == sid))).mappings().one()
                exports = (await db.execute(select(NativeRequest.__table__).where(
                    NativeRequest.case_id == api.case_id, NativeRequest.kind == 'terminal.export'))).mappings().all()
            assert len(exports) == 1
            export = exports[0]
            assert session['phase'] != 'closed' and not json.loads(session['process_json']).get('export')
            record('inspection_did_not_invent_publication_or_terminal_completion')

            if mode == 'recover':
                from sandbox_recovery_checks import verify_recovery
                await verify_recovery(api, engine, sid, proof, export, original_args, stranger['token'], record)
                report['recovery_performed'] = True
                report['passed'] = True
                return

            async def fault(table, identifier, column, changed, original, reasons):
                try:
                    async with engine.begin() as db:
                        await db.execute(update(table).where(table.c.id == identifier).values({column: changed}))
                    value = await call('GET', endpoint)
                    assert value['status'] == 'blocked' and value['reason'] in reasons, value
                    return value
                finally:
                    async with engine.begin() as db:
                        await db.execute(update(table).where(table.c.id == identifier).values({column: original}))

            journal = json.loads(export['children_json'])
            child = next(iter(journal['children'].values()))
            child.update(phase='preparing', identity=None, stop=None, error=None, ended_at=None)
            await fault(NativeRequest.__table__, export['id'], 'children_json', json.dumps(journal),
                        export['children_json'], {'process_registration_incomplete'})
            record('unknown_preparing_child_refused', 'protocol_fault')
            profile = json.loads(export['profile_json'])
            del profile['session_id']
            await fault(NativeRequest.__table__, export['id'], 'profile_json', json.dumps(profile),
                        export['profile_json'], {'export_binding_invalid'})
            await fault(NativeRequest.__table__, export['id'], 'profile_json', '{invalid',
                        export['profile_json'], {'ownership_records_unverified'})
            record('missing_and_corrupt_export_binding_refused', 'protocol_fault')
            profile = json.loads(export['profile_json'])
            profile['session_id'] = str(uuid.uuid4())
            await fault(NativeRequest.__table__, export['id'], 'profile_json', json.dumps(profile),
                        export['profile_json'], {'export_binding_invalid'})
            record('changed_session_binding_cannot_hide_exporter_of_same_image', 'protocol_fault')
            profile = json.loads(session['profile_json'])
            profile['source_prefix'] += '-changed'
            await fault(NativeSession.__table__, sid, 'profile_json', json.dumps(profile),
                        session['profile_json'], {'case_source_changed'})
            record('changed_case_source_refused', 'protocol_fault')
            new_owner = identity.observe(api.proc.pid)
            value = await fault(NativeRequest.__table__, export['id'], 'owner_identity_json', json.dumps(new_owner),
                                export['owner_identity_json'], {'owner_still_active'})
            assert any(c['role'].startswith('export_owner:') and c['state'] == 'match' for c in value['checks'])
            record('live_export_owner_refused_after_terminal_owner_death', 'protocol_fault', evidence=value)
            new_owner['creation_time_100ns'] = str(int(new_owner['creation_time_100ns']) + 1)
            await fault(NativeSession.__table__, sid, 'owner_identity_json', json.dumps(new_owner),
                        session['owner_identity_json'], {'owner_identity_unverified'})
            record('same_pid_with_wrong_birth_refused', 'protocol_fault')
            final = await call('GET', endpoint)
            assert final['status'] == 'processes_exited', final
            report['passed'] = True
    finally:
        await engine.dispose()
        (api.root / 'checks.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        print('RECOVERY_INSPECTION_EVIDENCE=' + str(api.root / 'checks.json'), flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--storage', choices=('fs', 's3'), default='fs')
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--mode', choices=('owner-death', 'closed'), default='owner-death')
    args = parser.parse_args()

    def run(store=None, url=None, group=None):
        return asyncio.run(verify(store, url, args.mode))

    def database(store):
        if args.database == 'sqlite':
            return run(store)
        from mixed_read_capacity_checks import postgres
        return asyncio.run(postgres(store, 'recovery-inspection', run))

    if args.storage == 'fs':
        database(None)
    else:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
            database(store)
