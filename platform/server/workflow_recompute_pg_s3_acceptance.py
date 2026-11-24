"""Real PostgreSQL/SeaweedFS recompute conflicts and a cold API restart."""
import argparse
import asyncio
from functools import partial
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid


SERVER = Path(__file__).resolve().parent
ROOT = SERVER.parents[1]
RUN_DIR = 'C-run/conduction'
OWNER_PATH = '/fixture/recompute-owner'
OWNER_PROBE = '''
import os
from fastapi import Depends
from app.deps import get_current_user
from app.services.process_identity_native import observe

@app.get('/api/v1/fixture/recompute-owner')
async def recompute_test_api_owner(user=Depends(get_current_user)):
    return observe(os.getpid())

@app.post('/api/v1/fixture/recompute-owner:exit')
async def recompute_test_api_exit(user=Depends(get_current_user)):
    os._exit(19)
'''
ALIAS_OBSERVER = '''
import json
from app.db import SessionLocal
from app.models import ArtifactPublication
from app.services import artifact_publication
from sqlalchemy import select

original_repair = artifact_publication.repair_aliases

async def observe_mesh_aliases(owner):
    async with SessionLocal() as db:
        row = await db.scalar(select(ArtifactPublication).where(
            ArtifactPublication.attempt_id == owner.attempt_id, ArtifactPublication.kind == 'mesh'))
        if row is not None:
            assert row.status == 'committed'
            record = {'id': row.id, 'attempt_id': row.attempt_id, 'job_id': owner.job_id,
                      'immutable_path': row.immutable_path, 'manifest_sha256': row.manifest_sha256}
            staged = root / 'mesh-aliases-staged.json'
            staged.write_text(json.dumps(record), encoding='utf8')
            staged.replace(root / 'mesh-aliases-pending.json')
    if row is not None:
        async with asyncio.timeout(90):
            while not (root / 'mesh-aliases-release').exists():
                await asyncio.sleep(.03)
    return await original_repair(owner)

artifact_publication.repair_aliases = observe_mesh_aliases
'''


def digest(value):
    return hashlib.sha256(value).hexdigest()


async def postgres_snapshot(url, case_id):
    import asyncpg
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    db = await asyncpg.connect(host=parsed.host, port=parsed.port or 5432, user=parsed.username,
                               password=parsed.password, database=parsed.database, timeout=10)
    queries = {
        'jobs': 'SELECT id,status,exit_code,log_s3_key FROM jobs WHERE case_id=$1 ORDER BY id',
        'attempts': 'SELECT a.id,a.job_id,a.phase,a.process_identity_json,a.input_manifest_json,a.output_manifest_json '
                    'FROM job_attempts a JOIN jobs j ON a.job_id=j.id WHERE j.case_id=$1 ORDER BY a.job_id,a.number',
        'runs': 'SELECT id,status,version FROM workflow_runs WHERE case_id=$1 ORDER BY id',
        'events': 'SELECT id,seq,type,attempt_id,payload_json FROM durable_events WHERE case_id=$1 ORDER BY seq',
    }
    try:
        assert await db.fetchval('SELECT current_database()') == parsed.database
        source = await db.fetchval('SELECT s3_prefix FROM cases WHERE id=$1', case_id)
        active = dict(await db.fetchrow('SELECT p.id,p.attempt_id,p.status,p.generation,p.immutable_path,p.manifest_sha256 '
            'FROM artifact_publication_heads h JOIN artifact_publications p ON h.active_publication_id=p.id '
            "WHERE h.case_id=$1 AND h.kind='mesh'", case_id))
        tables = {}
        for name, sql in queries.items():
            rows = [dict(row) for row in await db.fetch(sql, case_id)]
            tables[name] = {'count': len(rows), 'sha256': digest(json.dumps(rows, sort_keys=True).encode('utf8'))}
        return {'database': parsed.database, 'case_prefix': source, 'mesh_head': active, 'tables': tables}
    finally:
        await db.close()


def s3_snapshot(store, database):
    from contextlib import closing

    prefix = database['case_prefix'].rstrip('/')
    head = database['mesh_head']
    publication = prefix + '/' + head['immutable_path']

    def read(key):
        with closing(store.client.get_object(Bucket=store.bucket, Key=key)['Body']) as body:
            return body.read()

    body = read(publication + '/manifest.json')
    assert digest(body) == head['manifest_sha256']
    manifest = json.loads(body)
    assert manifest['publication_id'] == head['id'] and manifest['attempt_id'] == head['attempt_id']
    assert manifest['storage']['backend'] == 's3'
    files = {}
    for path in (f'{RUN_DIR}/constant/polyMesh/points', f'{RUN_DIR}/blockMesh.log', f'{RUN_DIR}/checkMesh.log'):
        value = read(publication + '/files/' + path)
        assert value and digest(value) == manifest['files'][path]['sha256']
        files[path] = {'size': len(value), 'sha256': digest(value)}
    return {'bucket': store.bucket, 'endpoint': store.endpoint, 'publication_manifest_sha256': digest(body),
            'source_bytes': digest(read(prefix + '/0-caseDict/caseDict')), 'files': files}


def exit_api(api):
    from app.services.process_identity_native import probe

    birth = api.call('GET', OWNER_PATH)
    assert probe(birth)['state'] == 'match'
    try:
        api.call('POST', OWNER_PATH + ':exit')
    except OSError:
        pass
    else:
        raise AssertionError('The API unexpectedly answered its self-exit request')
    assert api.proc.wait(timeout=20) == 19
    observed = probe(birth)
    assert observed['state'] == 'exited', observed
    return {'pid': birth['pid'], 'exit_code': api.proc.returncode, 'observation': observed}


def observe_alias_window(api, store, flow, confirm, *, legacy_gate):
    from botocore.exceptions import ClientError
    from contextlib import closing

    from job_release_smoke import wait_file
    from professional_launch_smoke import settled
    from workflow_recompute_control_smoke import postgres_case_prefix

    evidence = api.root / 'mesh-alias-window.json'
    release = api.root / 'mesh-aliases-release'
    record = {'passed': False, 'legacy_gate': legacy_gate}
    try:
        pending = api.root / 'mesh-aliases-pending.json'
        wait_file(pending, seconds=110)
        publication = json.loads(pending.read_text(encoding='utf8'))
        prefix = asyncio.run(postgres_case_prefix(api.database_url, api.case_id)).rstrip('/')
        immutable = prefix + '/' + publication['immutable_path']

        def read(s3, key):
            with closing(s3.get_object(Bucket=store.bucket, Key=key)['Body']) as stream:
                return stream.read()

        manifest = read(store.raw_client, immutable + '/manifest.json')
        assert digest(manifest) == publication['manifest_sha256']
        files = json.loads(manifest)['files']
        path = min(files)
        immutable_bytes = read(store.raw_client, immutable + '/files/' + path)
        assert digest(immutable_bytes) == files[path]['sha256']
        target = prefix + '/' + path
        try:
            read(store.client, target)
        except ClientError as error:
            status = error.response['ResponseMetadata']['HTTPStatusCode']
            assert status == 404
        else:
            raise AssertionError('Expected the first mesh alias to be absent before repair')
        record.update(publication=publication, first_alias=path, before_release_status=status,
                      immutable_sha256=digest(immutable_bytes))
        expected_status = 500 if legacy_gate else 409
        response = confirm(flow['id'], 'confirm_C', expect=expected_status)
        record['confirmation_status'] = expected_status
        record['confirmation_body'] = response.decode() if isinstance(response, bytes) else response
        if not legacy_gate:
            assert response['detail']['code'] == 'evidence_invalid' and '正在同步' in response['detail']['message']
        release.touch()
        mesh = settled(api, publication['job_id'], timeout=120)
        completion = mesh['attempt']['outputs']['completion']
        assert completion['state'] == 'settled' and completion['alias_error'] is None
        assert read(store.client, target) == immutable_bytes
        if not legacy_gate:
            before = store.client.head_object(Bucket=store.bucket, Key=target)
            store.client.delete_object(Bucket=store.bucket, Key=target, IfMatch=before['ETag'])
            try:
                refused = confirm(flow['id'], 'confirm_C', expect=409)['detail']
                assert refused['code'] == 'evidence_invalid' and path in refused['message'], refused
                record['missing_after_settlement'] = refused
            finally:
                store.client.put_object(Bucket=store.bucket, Key=target, Body=immutable_bytes)
        record.update(passed=True, settled_completion=completion, alias_hash_after_release=digest(immutable_bytes),
                      checks=['committed_immutable_mesh_visible_before_logical_alias',
                              'confirmation_waits_for_compatibility_alias_and_recovers_after_settlement'])
    finally:
        release.touch(exist_ok=True)
        evidence.write_text(json.dumps(record, indent=2), encoding='utf8')
    return record


def observe_restart(api, store, source, done, payload):
    from app.services.owned_process import probe_native
    from app.services.process_identity_native import probe
    from professional_launch_smoke import settled
    from testsupport import IsolatedServer

    mesh = next(step for step in done['steps'] if step['key'] == 'mesh')
    attempt = mesh['attempts'][0]
    job = settled(api, attempt['job_id'], timeout=120)
    assert job['status'] == 'succeeded' and job['attempt']['process_stop']['verified']
    assert job['attempt']['workspace']['storage']['backend'] == 's3'
    birth = job['attempt']['process_identity']
    stopped = {'host': probe(birth['host'])}
    if birth['native'] is not None:
        stopped['wsl'] = asyncio.run(probe_native(birth['native']['distribution'], birth['native']['process']))
    assert all(state['state'] == 'exited' for state in stopped.values()), stopped

    source_path = '/workflow-runs/' + source['id']
    recompute_path = '/workflow-runs/' + done['id']
    before = asyncio.run(postgres_snapshot(api.database_url, api.case_id))
    objects = s3_snapshot(store, before)
    previous_source = api.call('GET', source_path)
    current_recompute = api.call('GET', recompute_path)
    mesh_read = api.call('GET', api.prefix + '/mesh/results')
    current_dict = api.call('GET', api.prefix + '/caseDict')
    assert previous_source == source and current_recompute['status'] == 'succeeded'
    assert mesh_read['publication']['id'] == before['mesh_head']['id']
    assert mesh_read['publication']['status'] == 'committed' and mesh_read['toolchain']['mesh_source'] == 'blockMesh'
    assert before['tables']['runs']['count'] == 2

    original = exit_api(api)
    replacement = IsolatedServer('workflow-recompute-restart', app='recompute_peer:app', database_url=api.database_url)
    settings = {**store.settings(), 'VIBEFLOW_JWT_SECRET': api.env['VIBEFLOW_JWT_SECRET']}
    script = 'import os\nos.environ.update(' + repr(settings) + ')\nfrom app.main import app\n' + OWNER_PROBE
    (replacement.root / 'recompute_peer.py').write_text(script, encoding='utf8')
    replacement.token = api.token
    assert not (replacement.root / 'files').exists()
    with replacement:
        assert original['pid'] != replacement.call('GET', OWNER_PATH)['pid']
        assert replacement.env['VIBEFLOW_STORAGE_FS_ROOT'] != api.env['VIBEFLOW_STORAGE_FS_ROOT']
        assert replacement.call('GET', source_path) == previous_source
        assert replacement.call('GET', recompute_path) == current_recompute
        assert replacement.call('GET', api.prefix + '/mesh/results') == mesh_read
        prior = replacement.call('POST', source_path + ':recompute', payload,
                                 extra_headers={'Idempotency-Key': 'one-recompute'})
        assert prior['id'] == done['id']
        assert replacement.call('GET', api.prefix + '/caseDict') == current_dict
        outsider = replacement.call('POST', '/auth/register', {
            'email': uuid.uuid4().hex + '@example.com', 'name': 'Unrelated user', 'password': 'isolated-recompute-test'})
        outside = {'Authorization': 'Bearer ' + outsider['token'], 'Idempotency-Key': 'not-owned'}
        replacement.call('POST', source_path + ':recompute', payload, expect=404, extra_headers=outside)
        after = asyncio.run(postgres_snapshot(api.database_url, api.case_id))
        assert after == before, {'before': before, 'after': after}
        assert s3_snapshot(store, after) == objects
        replaced = exit_api(replacement)
    assert replacement.proc.poll() is not None
    return {'checks': ['actual_blockMesh_scope_verified_stopped', 'cold_api_recovers_original_and_recomputed_workflows',
                      'unrelated_account_cannot_recompute_after_restart',
                      'replay_preserves_pg_job_attempt_event_and_publication_rows_and_s3_bytes'],
            'job_id': job['id'], 'attempt_id': attempt['id'], 'source_run': source['id'], 'recomputed_run': done['id'],
            'native_exit': stopped, 'process_stop': job['attempt']['process_stop'], 'postgres': after, 'objects': objects,
            'original_api': original, 'replacement_api': {**replaced, 'evidence': str(replacement.root)}}


async def verify(*, legacy_gate=False):
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import Settings
    from s3testsupport import ObjectStore
    from workflow_migration_smoke import alembic
    from workflow_recompute_control_smoke import verify as recompute_control

    url = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1', 'localhost', '::1'}
    database = 'tmp_vf_recomputeobject_' + uuid.uuid4().hex
    evidence = Path(tempfile.mkdtemp(prefix='workflow-recompute-pg-s3-', dir=ROOT / 'tmp')) / 'result.json'
    print('RECOMPUTE_PG_S3_EVIDENCE ' + str(evidence), flush=True)
    report = {'passed': False, 'database': database, 'database_dropped': False, 'legacy_gate': legacy_gate,
              'model_calls': 0, 'mock_llm_calls': 0, 'production_deployment_touched': False}
    db = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                               password=url.password, database='postgres', timeout=10)
    created = False
    store = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed', volume_max=6)
    report['object_store_evidence'] = str(store.root)
    try:
        await db.execute('CREATE DATABASE "' + database + '"')
        created = True
        isolated = url.set(database=database).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, isolated, evidence.parent, 'upgrade', 'head')
        with store:
            report['control'] = await asyncio.to_thread(recompute_control, isolated, store,
                before_mesh_confirmation=partial(observe_alias_window, legacy_gate=legacy_gate),
                after_completed=observe_restart, fixture_extension=OWNER_PROBE + ALIAS_OBSERVER)
        assert store.proc is not None and store.proc.poll() is not None
        report['passed'] = True
    finally:
        try:
            if created:
                assert await db.fetchval('SELECT current_database()') == 'postgres'
                await db.execute('DROP DATABASE "' + database + '"')
                report['database_dropped'] = True
        finally:
            await db.close()
            report['object_store_exited'] = store.proc is not None and store.proc.poll() is not None
            evidence.write_text(json.dumps(report, indent=2), encoding='utf8')
    print('RECOMPUTE_PG_S3_OK ' + str(evidence), flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-legacy-gate', action='store_true', help='Record the earlier uncaught S3 alias 404')
    args = parser.parse_args()
    enforce([Path(__file__)])
    asyncio.run(verify(legacy_gate=args.observe_legacy_gate))
