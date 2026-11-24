"""Turn delivery waits for scope retirement and S3 staging finalization.

Uses the content-ETag file-backed S3 facade, never a remote S3 service.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from codex_turn_api_smoke import CONTROLLED, WORKER
from codex_session_smoke import CONTROLLED as BASE, wait_for
from codex_session_staging_smoke import CONTROLLED as STAGING
from app.services import process_identity_native as identity

STAGING_WORKER = WORKER.replace("    if status=='completed':", "    if status=='completed':\n        target=Path.cwd()/'Ref';target.mkdir(exist_ok=True);(target/'cli-data.txt').write_text('TURN',encoding='utf8')", 1)


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None:
        alembic(url, folder, 'upgrade', 'head')
    api = IsolatedServer('cli-turn-staging', app='turn_staging:app', database_url=url)
    (api.root / 'cli.py').write_text(STAGING_WORKER, encoding='utf8')
    (api.root / 'turn_staging.py').write_text(CONTROLLED + STAGING[len(BASE):], encoding='utf8')
    checks, receipts = [], []
    with api:
        api.create_case('Native turn S3 delivery')
        workspace = api.call('GET', '/workspaces')[0]['id']
        provider_id = api.call('POST', '/fixture/profile')['id']
        api.call('POST', '/fixture/staging-enable')
        for mode in ('normal', 'conflict', 'case_unavailable', 'source_rebound', 'sync_hold', 'cleanup_retry', 'cleanup_failure', 'cancelled'):
            api.call('POST', '/fixture/mode/' + mode)
            case = api.call('POST', f'/workspaces/{workspace}/cases', {'name': mode + uuid.uuid4().hex[:6]})
            api.call('PUT', f"/cases/{case['id']}/file", {'path': 'Ref/cli-data.txt', 'content': 'BEFORE'})
            conv = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'full_access'})
            session = api.call('POST', '/codex/sessions', {'case_id': case['id'], 'conversation_id': conv['id'],
                'provider_profile_id': provider_id, 'provider_wire_api': 'responses'})
            held = mode in {'conflict', 'case_unavailable', 'source_rebound', 'cancelled'}
            turn = api.call('POST', '/codex/sessions/' + session['id'] + '/turns', {
                'content': 'command' if held else 'detached', 'client_message_id': uuid.uuid4().hex})
            path = '/codex/turns/' + turn['id']
            if held:
                approval = wait_for(lambda: api.call('GET', path + '/approvals'), bool)[0]
                if mode != 'cancelled':
                    api.call('POST', '/fixture/staging-change/' + session['id'] + '/' + mode)
                    if mode == 'conflict':
                        api.call('POST', path + '/approvals/' + approval['id'] + ':respond', {'decision': 'accept'})
                    else:
                        try:
                            api.call('POST', path + '/approvals/' + approval['id'] + ':respond', {'decision': 'accept'}, expect=400)
                        except AssertionError as error:
                            # The renewal task may retire the session first;
                            # both reauthorization rejection and stale owner
                            # conflict forbid the native response.
                            assert 'got 409' in str(error), str(error)
                else:
                    api.call('POST', path + ':cancel')
            if mode == 'sync_hold':
                wait_for(lambda: (api.root / 'sync-started').exists(), bool)
                before = api.call('GET', '/fixture/turn/' + turn['id'])
                assert before['turn']['phase'] == 'finalizing' and before['control'] == 'active', before
                assert before['events'][-1]['type'] == 'request.completed'
                assert identity.probe(json.loads((api.root / 'leaf.json').read_text()))['state'] == 'exited'
                staged = api.call('GET', '/fixture/staging/' + session['id'])
                assert staged['remote'] == 'BEFORE' and staged['directory']
                (api.root / 'release-sync').touch()
            if mode in {'case_unavailable', 'source_rebound'}:
                # Failed approval reauthorization retains the waiting owner;
                # explicit cancellation still joins it and never syncs writes.
                api.call('POST', path + ':cancel')
            value = wait_for(lambda: api.call('GET', path), lambda row: row['phase'] in {'completed', 'failed', 'cancelled'}, seconds=40)
            details = api.call('GET', '/fixture/turn/' + turn['id'])
            stage = api.call('GET', '/fixture/staging/' + session['id'])
            receipts.append({'mode': mode, 'details': details, 'stage': stage})
            successful = mode in {'normal', 'sync_hold', 'cleanup_retry'}
            assert (value['phase'] == 'completed') is successful, (mode, value)
            assert value['cleanup_verified'], (mode, value)
            assert details['events'][-1]['type'] == 'stream.done'
            if successful:
                assert stage['remote'] == 'TURN' and not stage['directory'] and not stage['manifest']
                assert details['control'] == 'idle'
            else:
                assert details['control'] == 'recovery_required' and stage['directory']
                assert stage['remote'] == ('REMOTE' if mode == 'conflict' else 'TURN' if mode == 'cleanup_failure' else 'BEFORE')
            assert not any('.private-cli' in name or name.endswith('auth.json') for name in stage['remote_paths'])
            checks.append(mode + '_turn_delivery_and_staging_receipt')
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'controlled_s3_facade': True,
              'remote_s3': False, 'evidence': str(api.root)}
    (api.root / 'turn-staging-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
    (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='cli-turn-staging-evidence-', dir=SERVER.parents[1] / 'tmp'))
    results = {'sqlite': await asyncio.to_thread(run_database, None, root / 'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_turnstage_' + uuid.uuid4().hex
        assert re.fullmatch('tmp_vf_turnstage_[0-9a-f]{32}', name)
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                      password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            results['postgresql'] = await asyncio.to_thread(run_database,
                url.set(database=name).render_as_string(hide_password=False), root / 'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (root / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'counts': {key: value['count'] for key, value in results.items()}, 'evidence': str(root)}))


if __name__ == '__main__':
    asyncio.run(main())
