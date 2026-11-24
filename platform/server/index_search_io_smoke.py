"""Actual case search during business writes and slow index storage I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import httpx

from case_copy_source_lock_smoke import RUNTIME, read_observation, writer_available
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


HELD = 'Ref/index-aaa-source.txt'
EDIT = 'Ref/index-version.txt'
SIZE = 4 * 1024 * 1024
OLD = 'indexbeforeversion'
NEW = 'indexorangeversion'
TIMEOUT = 8

FIXTURE = '''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends

from app.deps import get_current_user
from app.main import app
from app.services import durable_events, indexsvc, process_identity_native, request_deadline


root = Path(__file__).parent
observed = ContextVar('search_observed', default=None)
real_body = indexsvc.get_bytes
real_entries = indexsvc._entries


def record(name, value):
    path = root / name
    scratch = path.with_suffix('.writing')
    scratch.write_text(json.dumps(value, sort_keys=True), encoding='utf8')
    scratch.replace(path)


async def keep_durable_events_pending(*args, **kwargs):
    return 0


durable_events.deliver_pending = keep_durable_events_pending


def event(name):
    state = observed.get()
    if state is not None and state.get('expired_at_release'):
        state['io_started_after_expiry'].append(name)
    return state


def watch_entries(prefix):
    event('inventory')
    return real_entries(prefix)


def watch_body(prefix, path, **options):
    state = event('body')
    try:
        data = real_body(prefix, path, **options)
    except Exception as error:
        if state is not None:
            state['read_failure'] = {'type':type(error).__name__, 'path':path,
                's3_status':getattr(error, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode')}
        raise
    if state is None or path != 'Ref/index-aaa-source.txt' or 'bytes' in state:
        return data
    deadline = request_deadline.current()
    state.update(bytes=len(data), deadline_active=deadline is not None,
                 remaining=deadline.remaining() if deadline is not None else None)
    name = state['mode']
    record(name + '-read.json', state)
    until = time.monotonic() + 25
    while not (root / (name+'-release')).exists():
        if time.monotonic() >= until:
            raise TimeoutError('Search I/O observation was not released')
        time.sleep(.03)
    state['expired_at_release'] = deadline.expired if deadline is not None else False
    return data


indexsvc._entries = watch_entries
indexsvc.get_bytes = watch_body


@app.get('/fixture/search-process')
def process(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())


class WatchSearch:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        mode = dict(scope.get('headers', ())).get(b'x-fixture-observe-search')
        if not scope.get('path','').endswith('/search') or mode not in {b'edit',b'expiry',b'remove'}:
            return await self.app(scope, receive, send)
        state = {'mode': mode.decode('ascii'), 'io_started_after_expiry': []}
        token = observed.set(state)

        async def capture(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            await send(message)

        try:
            return await self.app(scope, receive, capture)
        finally:
            observed.reset(token)
            record(state['mode'] + '-end.json', state)


app = WatchSearch(app)
'''


def expire_content_verification(index):
    with closing(sqlite3.connect(index)) as db:
        updated = db.execute("UPDATE meta SET value = '0' WHERE key = 'content_verified_at'")
        assert updated.rowcount == 1
        db.commit()


def snapshot(index):
    with closing(sqlite3.connect(index.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('building_uuid','content_verified_at')"))
        content, row = db.execute('SELECT content,rowid FROM file_fts WHERE path=?', (EDIT,)).fetchone() or (None,None)
        return {**meta, 'text':content, 'edit_rowid':row}


def hit(client, path, marker):
    result = client.get(path, params={'q':marker})
    assert result.status_code == 200, result.text
    return result.json()


def paused_request(api, client, path, marker, mode, while_paused):
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.get, path, params={'q':marker},
                                headers={'X-Fixture-Observe-Search':mode})
        try:
            block = read_observation(api, mode+'-read.json')
            assert not pending.done() and block['bytes'] == SIZE, block
            observation = while_paused(pending, block)
        finally:
            (api.root/(mode+'-release')).touch()
        result = pending.result(timeout=60)
    trace = read_observation(api, mode+'-end.json')
    assert trace['status'] == result.status_code, (trace, result.text)
    return result, observation, trace


def exercise(api, report, baseline):
    api.create_case('Case search storage acceptance')
    report['api_process'] = api.call('GET', '/fixture/search-process', api_prefix=False)
    assert len(OLD) == len(NEW)
    api.put(HELD, 'engineering held text\n'.ljust(SIZE))
    api.put(EDIT, OLD)
    api.call('POST', api.prefix+'/index:rebuild', {})
    index = api.root / 'index' / f'case-{api.case_id}.sqlite'
    expire_content_verification(index)
    prior = report['initial_index'] = snapshot(index)
    assert prior['text'] == OLD and prior['content_verified_at'] == '0', prior
    path = api.prefix + '/search'
    headers = {'Authorization':'Bearer '+api.token}
    with (httpx.Client(base_url=api.base+'/api/v1', headers=headers, timeout=60, trust_env=False) as search,
          httpx.Client(base_url=api.base+'/api/v1', headers=headers, timeout=60, trust_env=False) as edit):
        def save_while_reading(_pending, block):
            proof = {'block':block, 'sqlite_writer_available':writer_available(api)}
            response = edit.put(api.prefix+'/file', json={'path':EDIT, 'content':NEW,
                'expected_sha256':hashlib.sha256(OLD.encode()).hexdigest()}, timeout=20)
            proof['save'] = {'status':response.status_code, 'document':response.json()}
            return proof

        result, proof, trace = paused_request(api,search,path,OLD,'edit',save_while_reading)
        proof.update(status=result.status_code, reply=result.json(), trace=trace)
        report['concurrent_edit'] = proof
        assert proof['sqlite_writer_available'] is not baseline, proof
        assert proof['block']['deadline_active'] is not baseline, proof
        if baseline:
            assert result.status_code == 200 and result.json()['hits'][0]['path'] == EDIT, result.text
            assert proof['save']['status'] == 409 and proof['save']['document']['detail']['code'] == 'storage_quota_busy', proof
            return
        assert proof['save']['status'] == 200 and result.status_code == 409, proof
        assert proof['reply']['detail']['code'] == 'index_source_changed', proof
        report['stale_index'] = snapshot(index)
        assert report['stale_index'] == prior, report['stale_index']

        published = hit(search,path,NEW)
        assert published['ready'] and published['hits'][0]['path'] == EDIT
        assert published['hits'][0]['via'] == 'content' and not hit(search,path,OLD)['hits']
        report['new_index'] = snapshot(index)
        assert report['new_index']['text'] == NEW and report['new_index']['edit_rowid'] == prior['edit_rowid']
        assert report['new_index']['building_uuid'] != prior['building_uuid']

        expire_content_verification(index)
        before_expiry = snapshot(index)

        def wait_past_deadline(pending, block):
            assert block['deadline_active'] and 0 < block['remaining'] <= TIMEOUT, block
            time.sleep(block['remaining'] + .20)
            assert not pending.done(), 'Response completed before the actual reader drained'
            return {'remained_pending':True}

        result, proof, trace = paused_request(api,search,path,NEW,'expiry',wait_past_deadline)
        proof.update(status=result.status_code, reply=result.json(), trace=trace)
        report['expiry'] = proof
        assert result.status_code == 504 and proof['reply']['detail']['code'] == 'case_search_timeout', proof
        assert trace['expired_at_release'] and not trace['io_started_after_expiry'], trace
        report['index_after_expiry'] = snapshot(index)
        assert report['index_after_expiry'] == before_expiry, report['index_after_expiry']
        assert hit(search,path,NEW) == published
        report['index_after_retry'] = snapshot(index)
        assert report['index_after_retry']['building_uuid'] == before_expiry['building_uuid']
        assert float(report['index_after_retry']['content_verified_at']) > 0

        expire_content_verification(index)
        before_removal = snapshot(index)

        def remove_while_reading(_pending, _block):
            response = edit.delete(api.prefix+'/file', params={'path':EDIT,
                'expected_sha256':hashlib.sha256(NEW.encode()).hexdigest()})
            assert response.status_code == 200, response.text
            raw = edit.get(api.prefix+'/raw', params={'path':EDIT})
            assert raw.status_code == 404, raw.text
            return {'remove_status':response.status_code, 'raw_status':raw.status_code}

        result, proof, trace = paused_request(api,search,path,NEW,'remove',remove_while_reading)
        proof.update(status=result.status_code, reply=result.json(), trace=trace)
        report['concurrent_removal'] = proof
        assert result.status_code == 409 and proof['reply']['detail']['code'] == 'index_source_changed', proof
        native_failure = trace['read_failure']
        expected = {'type':'ClientError', 's3_status':404} if report['backend'] == 's3' else {
            'type':'FileNotFoundError', 's3_status':None}
        assert native_failure == {'path':EDIT, **expected}, native_failure
        report['index_after_removal_conflict'] = snapshot(index)
        assert report['index_after_removal_conflict'] == before_removal
        assert not hit(search,path,NEW)['hits']
        report['index_after_removal_retry'] = snapshot(index)
        assert report['index_after_removal_retry']['text'] is None


def run(backend, baseline):
    from app.services import process_identity_native

    api = IsolatedServer('index-search-io-'+backend, app='index_search_fixture:app')
    receipt = api.root / 'index-search-io.json'
    report = {'passed':False, 'baseline':baseline, 'backend':backend, 'api_port':api.port,
              'real_model_calls':0, 'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == 's3' else nullcontext()
    print('INDEX_SEARCH_IO_EVIDENCE',api.root,flush=True)
    try:
        with context as store:
            config = store.settings() if store else {}
            if store is not None:
                report['store_receipt'] = str(store.root/'store.json')
            temporary = api.root/'native-temp'
            temporary.mkdir()
            config.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                          VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS=str(TIMEOUT))
            fixture = api.root/'index_search_fixture.py'
            try:
                fixture.write_text('import os\nos.environ.update('+repr(config)+')\n'+FIXTURE, encoding='utf8')
                with api:
                    exercise(api,report,baseline)
            finally:
                fixture.unlink(missing_ok=True)
        if store is not None:
            report['store_exit'] = store.proc.poll()
            assert report['store_exit'] == 0
        report['api_exit'] = api.proc.poll()
        assert report['api_exit'] is not None
        api.proc = None
        until = time.monotonic() + 5
        while True:
            state = report['api_identity_after'] = process_identity_native.probe(report['api_process'])
            if state['state'] == 'exited' or time.monotonic() >= until:
                break
            time.sleep(.05)
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1400]}
        raise
    finally:
        receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    print('INDEX_SEARCH_IO_PASS',receipt,flush=True)


if __name__ == '__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    baseline = os.environ.get('VIBEFLOW_INDEX_SEARCH_IO_BASELINE') == '1'
    run('fs',baseline)
    if not baseline:
        run('s3',False)
