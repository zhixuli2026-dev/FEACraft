"""Pause an actual caseDict history read and verify its database writer scope."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import partial
import json
import sqlite3
import sys
import time

from live_llm_job_repeat_acceptance import main


FIXTURE = '''
from contextlib import contextmanager
import time
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.services.file_read import FileRead

original_history_open = FileRead._open

class PausedHistory:
    def __init__(self, stream):
        self.stream = stream

    def read(self, size):
        block = self.stream.read(size)
        if block and not (root/'provenance-paused').exists():
            from app.services.request_deadline import current
            deadline = current()
            if deadline is not None:
                (root/'provenance-time-left').write_text(str(deadline.remaining()), encoding='utf-8')
            (root/'provenance-paused').write_text(str(len(block)), encoding='utf-8')
            stop = time.monotonic() + 30
            while not (root/'provenance-release').exists():
                if time.monotonic() >= stop:
                    raise TimeoutError('Actual provenance read was not released')
                time.sleep(.02)
        return block

@contextmanager
def observe_history(self, *args, **kwargs):
    path = self.location['Key'] if self.remote else self.path.as_posix()
    with original_history_open(self, *args, **kwargs) as (stream, expected):
        if path.endswith('/0-caseDict/provenance.jsonl') and (root/'pause-provenance').exists():
            stream = PausedHistory(stream)
        yield stream, expected

FileRead._open = observe_history

@app.post('/api/v1/testing/provenance-short-deadline')
async def short_provenance_deadline(user=Depends(get_current_user)):
    get_settings().storage_measurement_timeout_seconds = 4
    return {'seconds': 4}

from app.services import provenance
if hasattr(provenance,'select_provenance'):
    original_select_provenance = provenance.select_provenance
    selections = 0

    def observe_select(prefix):
        global selections
        if (root/'pause-provenance-recheck').exists():
            selections += 1
            if selections == 2:
                (root/'provenance-recheck-paused').touch()
                stop = time.monotonic() + 30
                while not (root/'provenance-recheck-release').exists():
                    if time.monotonic() >= stop:
                        raise TimeoutError('Actual history version check was not released')
                    time.sleep(.025)
        return original_select_provenance(prefix)

    provenance.select_provenance = observe_select
'''


def sql_available(api):
    if api.database_url is not None:
        import asyncpg
        from sqlalchemy.engine import make_url

        async def postgres():
            url = make_url(api.database_url)
            connection = await asyncpg.connect(host=url.host,port=url.port,user=url.username,
                password=url.password,database=url.database,timeout=5)
            try:
                async with connection.transaction():
                    actual = await connection.fetchval(
                        'SELECT case_id FROM case_event_counters WHERE case_id=$1 FOR UPDATE NOWAIT',api.case_id)
                    assert actual==api.case_id
                return True
            except asyncpg.LockNotAvailableError:
                return False
            finally:
                await connection.close()
        return asyncio.run(postgres())
    with sqlite3.connect(api.root/'test.sqlite', timeout=.2) as connection:
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
            return True
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
            return False


def scenario(api, *, baseline, deadline, **_):
    case = api.prefix + '/caseDict'
    large = 'actual engineering provenance ' * 10000
    for index in range(4):
        api.call('PUT', case + '/block', {'block':'intent','entries':{
            'decisionLog': {'trace': {'status':'accepted','text':large}},
            'userIntent': f'Thermal verification {index}'}})
    previous = api.call('GET', case)
    marker = api.root/'provenance-paused'
    (api.root/'pause-provenance').touch()
    read = api.prefix + '/provenance?limit=1'
    if deadline:
        assert api.call('POST', '/testing/provenance-short-deadline')['seconds']==4
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.call, 'GET', read, expect=504)
            try:
                wait_for(pending, marker)
                remaining = float((api.root/'provenance-time-left').read_text())
                assert 0 < remaining <= 4
                time.sleep(remaining + .2)
                assert not pending.done()
                assert sql_available(api)
            finally:
                (api.root/'provenance-release').touch()
            response = pending.result(timeout=15)
        assert response['detail']['code']=='case_provenance_timeout'
        current = api.call('GET', read)
        assert current['chain_valid'] and current['events'][0]['revision']==previous['dict_revision']
        result = {'passed':True,'drained_on_timeout':True,'http':504,'original_history_readable':True}
        (api.root/'provenance-capacity-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result
    updated = {'block':'intent','expected_revision':previous['dict_revision'],
               'entries': {'decisionLog': {'trace': {'status':'superseded','text':'New decision'}}}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(api.call, 'GET', read, expect=(200,409))
        try:
            wait_for(reading, marker)
            unlocked = sql_available(api)
            write = pool.submit(api.call, 'PUT', api.prefix+'/file',
                                {'path':'Ref/during-history.txt','content':'saved while reading'})
            try:
                saved = write.result(timeout=5)
            except FutureTimeout:
                saved = None
        finally:
            (api.root/'provenance-release').touch()
        observed = reading.result(timeout=35)
        assert write.result(timeout=35)['ok']
    assert unlocked == (not baseline)
    assert bool(saved) == (not baseline)
    first_read = int(marker.read_text())
    if baseline:
        assert observed['chain_valid'] and observed['events'][0]['revision']==previous['dict_revision']
        assert first_read > 1024*1024
    else:
        assert observed['chain_valid'] and observed['events'][0]['revision']==previous['dict_revision']
        assert first_read<=64*1024
        (api.root/'pause-provenance-recheck').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            stale = pool.submit(api.call, 'GET', read, expect=(200,409))
            try:
                wait_for(stale, api.root/'provenance-recheck-paused')
                committed = api.call('PUT', case+'/block', updated)
            finally:
                (api.root/'provenance-recheck-release').touch()
            rejected = stale.result(timeout=35)
        assert rejected['detail']['code']=='file_changed_during_read', rejected
    if baseline:
        committed = api.call('PUT', case+'/block', updated)
    current = api.call('GET', read)
    assert current['chain_valid'] and current['events'][0]['revision']==committed['dict_revision']
    assert current['intent_lifecycle']['current']['decisionLog']['trace']=='superseded'
    report = {'passed':True,'baseline':baseline,'sql_writer_available':unlocked,
              'case_saved_during_actual_read':bool(saved),'first_read_bytes':first_read,
              'concurrent_revision_rejected':not baseline,'current_revision_verified':True}
    (api.root/'provenance-capacity-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def wait_for(request, marker):
    stop = time.monotonic() + 12
    while not marker.exists():
        if request.done():
            raise AssertionError(request.result())
        if time.monotonic() >= stop:
            raise TimeoutError('The API did not reach the selected history observation')
        time.sleep(.025)


if __name__ == '__main__':
    from s3testsupport import ObjectStore, SERVER
    baseline = '--provenance-baseline' in sys.argv
    if baseline:
        sys.argv.remove('--provenance-baseline')
    deadline = '--provenance-deadline' in sys.argv
    if deadline:
        sys.argv.remove('--provenance-deadline')
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,deadline=deadline), fixture_override=FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary, volume_max=8, authenticated=True)))
