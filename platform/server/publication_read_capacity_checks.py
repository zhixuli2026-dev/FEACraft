"""Real S3 publication read admission and native Pandoc compatibility; no LLM."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

import httpx

from testsupport import IsolatedServer, SERVER

FIXTURE = '''
from pathlib import Path
import json
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import OwnerStorageReservation
from app.services import quota
root=Path(__file__).parent
original=quota.quotas_for
async def quotas(db,user):
 value=await original(db,user)
 return {**value,'storage_mb':0} if (root/'quota-zero').exists() else value
quota.quotas_for=quotas
@app.get('/api/v1/fixture/credit')
async def credit(measure:bool=False,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  rows=list(await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id)))
  result={'rows':[{'state':r.state,'receipt':json.loads(r.receipt_json)} for r in rows]}
  if measure:result['bytes']=(await quota._storage_usage(user)).bytes
  return result
'''

ROUTES = [
    ('GET', '/report/compose-result', 404), ('GET', '/report/compositions', 200),
    ('GET', '/report/derivatives', 200), ('GET', '/report/derivative?publication_id=missing', 404),
    ('GET', '/report/derivative-artifact?publication_id=missing&path=report.md', 404),
    ('HEAD', '/report/derivative-artifact?publication_id=missing&path=report.md', 404),
    ('GET', '/report/compose-artifact?path=E-report/report.md', 404),
    ('GET', '/report/results', 404), ('GET', '/report/publications', 200),
    ('GET', '/report/artifact?path=E-report/build/report.html', 404),
    ('GET', '/latex/results', 404), ('GET', '/latex/publications', 200),
    ('GET', '/latex/artifact?path=E-report/latex/build/report.pdf', 404),
    ('HEAD', '/latex/artifact?path=E-report/latex/build/report.pdf', 404),
    ('GET', '/latex/source?path=E-report/latex/main.tex', 404),
    ('GET', '/geometry/scene', 404), ('GET', '/geometry/validation', 404),
    ('GET', '/geometry/publications', 200), ('GET', '/mesh/results', 404),
    ('GET', '/mesh/publications', 200), ('GET', '/solve/results', 404),
    ('GET', '/solve/publications', 200), ('GET', '/postprocess/results', 404),
    ('GET', '/postprocess/publications', 200),
]
STATUS_ROUTES = [('GET', '/events', 200), ('GET', '/provenance', 200),
                 ('GET', '/job-batches', 200), ('GET', '/job-batches/' + '0' * 32, 404)]


def verify(store, database_url=None, native=True, group='publications'):
    api = IsolatedServer('publication-read-capacity', app='read_fixture:app', database_url=database_url)
    store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    (api.root / 'read_fixture.py').write_text('import os\nos.environ.update(' + repr(store.settings()) + ')\n' + FIXTURE, encoding='utf8')
    result = dict(passed=False, llm_calls=0, database='postgresql' if database_url else 'sqlite', group=group, checks=[])
    routes = ROUTES if group == 'publications' else STATUS_ROUTES

    def save():
        (api.root / 'checks.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')

    def settled():
        state = api.call('GET', '/fixture/credit')
        assert state['rows'] and all(r['state'] == 'settled' and r['receipt']['closed'] for r in state['rows']), state

    print('PUBLICATION_READ_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            api.create_case('Publication read capacity')
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=120) as client:
                before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                (api.root / 'quota-zero').touch()
                try:
                    for method, path, _ in routes:
                        response = client.request(method, api.prefix + path)
                        assert response.status_code == 429, (method, path, response.status_code, response.text)
                        settled()
                        result['checks'].append({'method': method, 'path': path, 'quota_refused': True})
                        save()
                    assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
                finally:
                    (api.root / 'quota-zero').unlink()
                print('PUBLICATION_READ_PASS zero_quota_' + str(len(routes)) + '_no_growth', flush=True)
                for method, path, expected in routes:
                    response = client.request(method, api.prefix + path)
                    assert response.status_code == expected, (method, path, response.status_code, response.text)
                    settled()
                    result['checks'].append({'method': method, 'path': path, 'normal_status': expected, 'settled': True})
                    save()
                print('PUBLICATION_READ_PASS normal_and_missing_' + str(len(routes)) + '_settled', flush=True)
                if group == 'status':
                    if native:
                        from professional_launch_smoke import settled as settled_job
                        batch = api.call('POST', api.prefix + '/jobs:batch', {
                            'commands': ['echo STATUS_READ_ONE', 'echo STATUS_READ_TWO'],
                            'max_concurrent': 1, 'type': 'shell', 'runtime': 'windows'}, expect=202)
                        for job in batch['jobs']:
                            done = settled_job(api, job['id'], timeout=180)
                            assert done['status'] == 'succeeded', done
                        detail = client.get(api.prefix + '/job-batches/' + batch['id']); detail.raise_for_status()
                        value = detail.json()
                        assert value['total'] == 2 and value['counts']['succeeded'] == 2 and value['status'] == 'succeeded', value
                        listed = client.get(api.prefix + '/job-batches'); listed.raise_for_status()
                        assert any(row['id'] == batch['id'] and row['counts']['succeeded'] == 2 for row in listed.json())
                        settled(); result['checks'].append({'actual_batch': batch['id'], 'actual_jobs': [j['id'] for j in batch['jobs']], 'detail_and_list': True})
                        print('PUBLICATION_READ_PASS actual_batch_two_jobs_detail_and_list', flush=True)
                else:
                    for path, document in [('A-geo/scene.json', {'objects': [], 'proof': 'remote-scene'}),
                                           ('A-geo/validation.json', {'ok': True, 'proof': 'remote-validation'})]:
                        api.put(path, json.dumps(document))
                        route = '/geometry/scene' if path.endswith('scene.json') else '/geometry/validation'
                        response = client.get(api.prefix + route); response.raise_for_status()
                        assert response.json() == document
                        settled(); result['checks'].append({'path': route, 'actual_remote_document': True})
                    for path, expected in [('/postprocess/results?publication_id=x&job_id=1', 400),
                                           ('/latex/publications?before_id=missing', 400),
                                           ('/report/compositions?before_id=missing', 400)]:
                        response = client.get(api.prefix + path)
                        assert response.status_code == expected, (path, response.status_code, response.text)
                        settled(); result['checks'].append({'path': path, 'invalid_selection_settled': True})
                    save()
                    if native:
                        from professional_launch_smoke import settled as settled_job
                        markdown = '# Actual publication read\n\nReal Pandoc HTML and DOCX.\n'
                        api.put('E-report/report.md', markdown)
                        submitted = api.call('POST', api.prefix + '/report:build', {})
                        job = settled_job(api, submitted['job_id'], timeout=180)
                        assert job['status'] == 'succeeded', job
                        report = client.get(api.prefix + '/report/results'); report.raise_for_status()
                        document = report.json(); publication = document['publication']['id']
                        html = client.get(api.prefix + '/report/artifact', params={'path': 'E-report/build/report.html', 'publication_id': publication})
                        html.raise_for_status(); assert b'Actual publication read' in html.content
                        history = client.get(api.prefix + '/report/publications'); history.raise_for_status()
                        assert any(r['id'] == publication and r['current'] for r in history.json())
                        missing = client.get(api.prefix + '/report/artifact', params={'path': 'E-report/not-published.txt', 'publication_id': publication})
                        assert missing.status_code == 404
                        api.put('E-report/report.md', markdown + '\nChanged source.\n')
                        stale = client.get(api.prefix + '/report/results', params={'publication_id': publication}); stale.raise_for_status()
                        assert stale.json()['publication']['stale']
                        prior = client.get(api.prefix + '/report/artifact', params={'path': 'E-report/build/report.html', 'publication_id': publication})
                        prior.raise_for_status(); assert prior.content == html.content
                        settled(); result['checks'].append({'actual_pandoc_publication': publication,
                            'job_id': job['id'], 'html_history_missing_artifact_and_stale_immutable_bytes': True})
                        print('PUBLICATION_READ_PASS actual_native_report_history_and_staleness', flush=True)
            result['passed'] = True; save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:1600]}
        save(); print('PUBLICATION_READ_FAILED ' + json.dumps(result['failure'], ensure_ascii=False), flush=True)
        return 1
    print('PUBLICATION_READ_OK ' + str(api.root / 'checks.json'), flush=True)
    return 0


async def postgres(store, native, group):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    url = make_url(Settings(_env_file=SERVER / '.env').database_url)
    assert url.drivername == 'postgresql+asyncpg' and url.host in {'localhost', '127.0.0.1', '::1'}
    name = 'tmp_vf_pubread_' + uuid.uuid4().hex
    report = Path(tempfile.mkdtemp(prefix='pubread-postgres-', dir=SERVER.parents[1] / 'tmp'))
    admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                 password=url.password, database='postgres', timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "' + name + '"'); created = True
        target = url.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, target, report, 'upgrade', 'head')
        code = await asyncio.to_thread(verify, store, target, native, group)
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "' + name + '"')
                assert not await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', name)
                (report / 'cleanup.json').write_text(json.dumps({'temporary_database_removed': True, 'database': name}), encoding='utf8')
                print('PUBLICATION_READ_PG_CLEANUP ' + str(report / 'cleanup.json'), flush=True)
        finally:
            await admin.close()
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--skip-native', action='store_true')
    parser.add_argument('--group', choices=('publications', 'status'), default='publications')
    args = parser.parse_args()
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
        code = asyncio.run(postgres(store, not args.skip_native, args.group)) if args.database == 'postgresql' else verify(store, native=not args.skip_native, group=args.group)
    sys.exit(code)
