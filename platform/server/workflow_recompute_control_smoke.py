"""Real geometry reuse and concurrent new-mesh admission on SQLite/PostgreSQL."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import tempfile
import uuid
import sqlite3

from app.services.casedict import set_block, set_workflow_state
from cae_benchmarks import thermal_files
from case_command_publication_smoke import FIXTURE as BASE
from geometry_publication_smoke import script
from job_release_smoke import wait_file
from professional_launch_smoke import settled
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from workflow_recompute_smoke import FIXTURE
from s3_core_workflow_smoke import wait


async def postgres_case_prefix(database_url, case_id):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text('SELECT s3_prefix FROM cases WHERE id = :id'), {'id': case_id})
            return result.scalar_one()
    finally:
        await engine.dispose()


def verify(database_url=None, store=None, *, before_mesh_confirmation=None, after_completed=None, fixture_extension=''):
    api = IsolatedServer('workflow-recompute-control', app='recompute_fixture:app', database_url=database_url)
    settings = 'import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
    (api.root/'recompute_fixture.py').write_text(settings+BASE+FIXTURE+fixture_extension, encoding='utf8')
    (api.root/'mode').write_text('normal')
    checks = []
    with api:
        api.create_case('Recompute admission control')
        saved = api.call('GET', api.prefix+'/caseDict')
        text = set_block(saved['content'], 'mesh', {'source': 'blockMesh', 'qualityTargets': {'maxNonOrtho': 70, 'policy': 'fail'}})
        api.call('PUT', api.prefix+'/caseDict', {'content': text, 'expected_revision': saved['dict_revision']})
        for path, content in thermal_files().items():
            api.put(path, content.replace('(20 2 2)', '(20 4 4)'))
        preview = api.put('A-geo/geometry.py', script(2))
        assert settled(api, preview['auto_preview_job'])['status'] == 'succeeded'
        saved = api.call('GET', api.prefix+'/caseDict')
        source = api.call('POST', api.prefix+'/workflow-runs', {'target': 'geometry', 'run_dir': 'C-run/conduction',
            'expected_revision': saved['dict_revision'], 'parameters': {'geometry_mode': 'preview'},
            'budget': {'timeout_seconds': 180, 'max_log_bytes': 4*1024*1024}})
        def confirm(identifier, key, *, expect=200):
            current = wait(api, identifier, key)
            step = next(s for s in current['steps'] if s['key'] == key)
            result = api.call('POST', api.prefix+'/caseDict:advance', {'state': step['confirmation_state'],
                'confirmed': True, 'expected_revision': step['confirmation_revision'],
                'workflow_step_id': step['id'], 'expected_step_version': step['version']}, expect=expect, raw=expect==500)
            if expect == 200:
                assert result['workflow_dispatch']['status'] == 'dispatched', result
            return result
        confirm(source['id'], 'confirm_B')
        source = wait(api, source['id'])
        # Workflow success can precede the producer's final compatibility
        # receipt. The concurrency test below requires a stable source epoch;
        # a late completion_visible event is correctly rejected as input_stale.
        for job in api.call('GET', api.prefix+'/jobs'):
            assert settled(api, job['id'])['status'] == 'succeeded'
        # Simulate an out-of-band file/DB mismatch in the owned disposable case.
        # A no-op A reset must not bypass the same mirror check as create_run.
        saved = api.call('GET', api.prefix+'/caseDict')
        api.call('POST', api.prefix+'/caseDict:advance', {'state': 'A', 'confirmed': True,
            'expected_revision': saved['dict_revision']})
        saved = api.call('GET', api.prefix+'/caseDict')
        original = saved['content']
        if store:
            if database_url:
                prefix = asyncio.run(postgres_case_prefix(database_url, api.case_id))
            else:
                with sqlite3.connect((api.root/'test.sqlite').resolve().as_uri()+'?mode=ro', uri=True) as db:
                    prefix = db.execute('SELECT s3_prefix FROM cases WHERE id=?', (api.case_id,)).fetchone()[0]
            key = prefix.rstrip('/')+'/0-caseDict/caseDict'
            def raw_write(content):
                store.client.put_object(Bucket=store.bucket, Key=key, Body=content.encode('utf8'))
            def raw_read():
                with closing(store.client.get_object(Bucket=store.bucket, Key=key)['Body']) as body:
                    return body.read()
        else:
            root = next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            target = root/'0-caseDict/caseDict'
            def raw_write(content):
                target.write_bytes(content.encode('utf8'))
            raw_read = target.read_bytes
        divergence = set_workflow_state(original, 'E')
        raw_write(divergence)
        try:
            error = api.call('POST', '/workflow-runs/'+source['id']+':recompute',
                {'target': 'mesh', 'expected_version': source['version'], 'expected_revision': saved['dict_revision'],
                 'confirmed_reset': True}, extra_headers={'Idempotency-Key': 'inconsistent-mirror'}, expect=409)
            assert error['detail']['code'] == 'input_stale', error
            assert len(api.call('GET', api.prefix+'/workflow-runs')) == 1
            assert raw_read() == divergence.encode('utf8')
        finally:
            raw_write(original)
        api.call('POST', api.prefix+'/caseDict:advance', {'state': 'B', 'confirmed': True,
            'expected_revision': saved['dict_revision']})
        checks.append('file_database_state_mismatch_rejects_before_reset_or_creation')
        saved = api.call('GET', api.prefix+'/caseDict')
        payload = {'target': 'mesh', 'expected_version': source['version'], 'expected_revision': saved['dict_revision'],
                   'confirmed_reset': True}
        endpoint = '/workflow-runs/'+source['id']+':recompute'
        def submit():
            return api.call('POST', endpoint, payload, extra_headers={'Idempotency-Key': 'one-recompute'})
        before_jobs = api.call('GET', api.prefix+'/jobs')
        (api.root/'recompute-hold').touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = pool.submit(submit), pool.submit(submit)
            try:
                wait_file(api.root/'recompute-ready-1', seconds=20)
                wait_file(api.root/'recompute-ready-2', seconds=20)
            finally:
                (api.root/'recompute-release').touch()
            a, b = left.result(timeout=30), right.result(timeout=30)
        (api.root/'recompute-hold').unlink()
        (api.root/'recompute-release').unlink()
        assert a['id'] == b['id'] != source['id']
        assert len(api.call('GET', api.prefix+'/workflow-runs')) == 2
        checks.append('concurrent_requests_create_one_owned_recomputation')
        geo = next(s for s in a['steps'] if s['key'] == 'geometry')
        assert geo['execution_mode'] == 'reuse' and geo['attempts'] == []
        assert len(api.call('GET', api.prefix+'/jobs')) == len(before_jobs)
        assert a['recomputation_reset']['from_state'] == 'B' and a['recomputation_reset']['to_state'] == 'A'
        checks.append('geometry_reuse_has_no_native_attempt_and_reset_is_audited')
        confirm(a['id'], 'confirm_B')
        if before_mesh_confirmation is not None:
            alias_evidence = before_mesh_confirmation(api, store, a, confirm)
            checks.extend(alias_evidence['checks'])
        confirm(a['id'], 'confirm_C')
        done = wait(api, a['id'])
        mesh = next(s for s in done['steps'] if s['key'] == 'mesh')
        assert len(mesh['attempts']) == 1 and mesh['attempts'][0]['job_status'] == 'succeeded'
        assert len(api.call('GET', api.prefix+'/jobs')) == len(before_jobs)+1
        assert not {'solve', 'postprocess', 'report'} & {s['key'] for s in done['steps']}
        checks.append('only_one_real_blockMesh_job_executes_for_the_mesh_target')
        assert api.call('GET', '/workflow-runs/'+source['id']) == source
        checks.append('source_workflow_is_unchanged')
        current = api.call('GET', api.prefix+'/caseDict')
        assert submit()['id'] == done['id']
        assert api.call('GET', api.prefix+'/caseDict') == current
        checks.append('completed_request_replay_does_not_reset_or_execute_again')
        api.call('POST', endpoint, dict(payload, expected_revision=current['dict_revision']),
            extra_headers={'Idempotency-Key': 'current-target'}, expect=409)
        assert len(api.call('GET', api.prefix+'/workflow-runs')) == 2
        checks.append('fresh_mesh_target_does_not_create_another_run')
        # Even an external edit that bypasses the event counter must not change
        # caseDict between plan observation and the final creation transaction.
        late_payload = dict(payload, target='report', expected_revision=current['dict_revision'])
        (api.root/'recompute-hold').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.call, 'POST', endpoint, late_payload,
                extra_headers={'Idempotency-Key': 'external-dictionary-race'}, expect=409)
            try:
                wait_file(api.root/'recompute-ready-3', seconds=30)
                external = current['content']+'\n// external bookkeeping edit during planning\n'
                raw_write(external)
                (api.root/'recompute-release').touch()
                error = future.result(timeout=30)
                assert error['detail']['code'] == 'input_stale', error
                assert 'dictionary changed during' in error['detail']['message'], error
                assert raw_read() == external.encode('utf8')
            finally:
                (api.root/'recompute-release').touch()
                raw_write(current['content'])
        assert api.call('GET', api.prefix+'/caseDict') == current
        assert len(api.call('GET', api.prefix+'/workflow-runs')) == 2
        checks.append('external_dictionary_edit_after_observation_rejects_without_new_run')
        if after_completed is not None:
            restart_evidence = after_completed(api, store, source, done, payload)
            checks.extend(restart_evidence['checks'])
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(api.root)}
    if before_mesh_confirmation is not None:
        result['alias_window'] = alias_evidence
    if after_completed is not None:
        result['restart'] = restart_evidence
    (api.root/'recompute-control-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--s3-only', action='store_true')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='workflow-recompute-control-evidence-', dir=SERVER.parents[1]/'tmp'))
    results = {} if args.s3_only else {'sqlite': await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost', '127.0.0.1', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_recompute_'+uuid.uuid4().hex
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            temporary = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, temporary, report, 'upgrade', 'head')
            results['postgresql'] = await asyncio.to_thread(verify, temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    if args.s3 or args.s3_only:
        from s3testsupport import ObjectStore
        from s3_typed_publication_smoke import RUNTIME
        with ObjectStore(executable=RUNTIME) as store:
            results['s3_sqlite'] = await asyncio.to_thread(verify, store=store)
    (report/'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('WORKFLOW_RECOMPUTE_CONTROL_OK '+json.dumps({'databases': list(results), 'evidence': str(report)}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
