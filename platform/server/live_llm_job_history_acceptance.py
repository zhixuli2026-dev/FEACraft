"""Verify public job history against actual SQL rows across an API process restart."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import time
import uuid

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Job
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_publication_restart_acceptance import FIXTURE, reboot


async def database_rows(api, *, legacy=False):
    engine = create_async_engine(api.env['VIBEFLOW_DATABASE_URL'])
    try:
        async with engine.begin() as db:
            if legacy:
                # Pre-attempt records may legitimately lack a finish timestamp.
                for stamp in (datetime(2021, 6, 1, 2, 3, 4, 123456, tzinfo=timezone.utc), None):
                    await db.execute(insert(Job).values(case_id=api.case_id, type='shell', command='legacy',
                                                        status='succeeded', exit_code=0, ended_at=stamp))
            rows = await db.execute(select(Job.id, Job.status, Job.exit_code, Job.ended_at)
                                    .where(Job.case_id == api.case_id))
            result = {}
            for identifier, status, code, stamp in rows:
                if stamp is not None and stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                result[identifier] = dict(status=status, exit_code=code, ended_at=stamp.timestamp() if stamp else None)
            return result
    finally:
        await engine.dispose()


def scenario(api, *, store=None, **_):
    jobs_url = api.prefix + '/jobs'
    owner = api.token
    barrier = api.root / 'history-command-started'
    put(api, 'Ref/wait.py',
        f'from pathlib import Path\nimport time\nPath({str(barrier)!r}).touch()\ntime.sleep(90)\n')

    def submit(command, key):
        limit = time.monotonic() + 30
        while True:
            value = api.call('POST', jobs_url, {'command': command}, extra_headers={'Idempotency-Key': key},
                             expect=(200, 409))
            if 'id' in value:
                return value['id']
            assert value['detail']['code'] in {'validation_busy', 'storage_quota_busy'}, value
            if time.monotonic() >= limit:
                raise TimeoutError('History acceptance submission stayed busy')
            time.sleep(.1)

    api.call('POST', jobs_url, {'command': ''}, expect=422)
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate = [pool.submit(submit, 'python Ref/wait.py', 'history-one-request') for _ in range(2)]
        held, replay = [f.result() for f in duplicate]
    assert held == replay
    wait_for(barrier.exists, 'Real job never started')
    active = api.call('GET', f'/jobs/{held}')
    assert active['status'] == 'running' and active['ended_at'] is None and active['exit_code'] is None
    assert asyncio.run(database_rows(api))[held] == dict(status='running', ended_at=None, exit_code=None)
    api.call('POST', f'/jobs/{held}:cancel')

    def stopped():
        row = api.call('GET', f'/jobs/{held}')
        return row['status'] == 'cancelled' and (row['attempt'].get('process_stop') or {}).get('verified')

    wait_for(stopped, 'Cancelled native process did not stop')
    good, bad = [submit('python -c "raise SystemExit('+str(code)+')"', uuid.uuid4().hex) for code in (0, 7)]
    api.wait_job(good)
    api.wait_job(bad, expected='failed')
    actual = asyncio.run(database_rows(api))
    assert set(actual) == {good, bad, held}
    assert actual[good]['exit_code'] == 0 and actual[bad]['exit_code'] == 7
    assert [actual[i]['status'] for i in (held, good, bad)] == ['cancelled', 'succeeded', 'failed']
    assert all(isinstance(r['ended_at'], float) for r in actual.values())
    expected = asyncio.run(database_rows(api, legacy=True))
    old_ids = set(expected) - set(actual)
    assert len(old_ids) == 2 and {expected[i]['ended_at'] for i in old_ids} == {
        None, datetime(2021, 6, 1, 2, 3, 4, 123456, tzinfo=timezone.utc).timestamp()}

    def history():
        def check(row):
            assert {key: row[key] for key in expected[row['id']]} == expected[row['id']], row['id']
            if row['id'] in old_ids:
                assert row['attempt'] is None
        listed = api.call('GET', jobs_url)
        assert {row['id'] for row in listed} == set(expected)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for row in pool.map(lambda i: api.call('GET', f'/jobs/{i}'), expected):
                check(row)
        for row in listed:
            check(row)

    history()
    pids = reboot(api)
    history()
    org = api.call('GET', '/org/mine')['org']['id']
    ws = '/workspaces/' + str(api.call('GET', '/workspaces')[0]['id'])
    email = uuid.uuid4().hex + '@example.com'
    viewer = api.call('POST', '/auth/register', {'email':email, 'name':'History reader',
                                                 'password':'isolated-test-123'})['token']
    api.token = None
    api.call('GET', jobs_url, expect=401)
    api.token = viewer
    api.call('GET', jobs_url, expect=404)
    hidden = api.call('GET', f'/jobs/{good}', expect=404)
    assert hidden == api.call('GET', '/jobs/2147483647', expect=404)
    api.token = owner
    api.call('POST', f'/org/{org}/members', {'email':email, 'role':'viewer'}, expect=201)
    api.token = viewer
    api.call('POST', f'/org/{org}/invitation:accept', {})
    api.token = owner
    share = {'org_id':org, 'role':'viewer', 'enabled':True}
    api.call('PUT', ws+'/sharing', share)
    api.token = viewer
    history()
    api.call('POST', f'/jobs/{good}:cancel', expect=404)
    api.token = owner
    api.call('PUT', ws+'/sharing', {**share, 'enabled':False})
    api.token = viewer
    api.call('GET', f'/jobs/{good}', expect=404)
    api.token = owner
    assert asyncio.run(database_rows(api)) == expected
    report = {'real_jobs': actual, 'historical_rows': {i:expected[i] for i in old_ids},
              'api_restart_pids':pids, 'concurrent_same_key':held == replay,
              'access':['anonymous','foreign','org_viewer','revoked'], 'storage':'s3' if store else 'fs',
              'database':'postgresql' if api.database_url else 'sqlite'}
    (api.root / 'job-history.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
