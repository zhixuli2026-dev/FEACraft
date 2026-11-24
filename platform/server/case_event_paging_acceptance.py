"""Exercise the actual case timeline endpoint with a large history and storage races."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

import httpx

from app.services.process_identity_native import probe
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


PATH = '0-caseDict/case-events.jsonl'
COUNT = 20480
READ_TIMEOUT = 20
RUNTIME = SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed'
FIXTURE = r'''
import os
from pathlib import Path
import time
import tracemalloc

from fastapi import Depends
from app.deps import get_current_user
from app.main import app
from app.services.file_read import FileRead
from app.services.process_identity_native import observe

root = Path(__file__).parent
original_chunks = FileRead.chunks

def hold_selected(self, *args, **kwargs):
    path = self.location['Key'] if self.remote else self.path.as_posix()
    if path.endswith('/case-events.jsonl') and (root/'hold').exists():
        (root/'hold').unlink()
        (root/'selected').touch()
        stop_at = time.monotonic() + 27
        while not (root/'release').exists():
            if time.monotonic() > stop_at:
                raise RuntimeError('Test did not release the selected timeline')
            time.sleep(.02)
    yield from original_chunks(self, *args, **kwargs)

FileRead.chunks = hold_selected

@app.on_event('startup')
async def birth():
    import json
    (root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.middleware('http')
async def measure(request, call_next):
    if not request.url.path.endswith('/events') or (root/'measurement-off').exists():
        return await call_next(request)
    tracemalloc.start()
    try:
        return await call_next(request)
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        (root/'peak.txt').write_text(str(peak))

@app.post('/fixture/stop', status_code=204)
async def stop(body: dict, user=Depends(get_current_user)):
    assert body['pid'] == os.getpid()
    os._exit(79)
'''


def make_history(path):
    with path.open('wb') as stream:
        # The Chinese character spans the reader's 64 KiB block boundary.
        header = b'{"sequence":0,"value":"'
        stream.write(header + b'x' * (65535 - len(header)) + '中"}\r\n'.encode('utf8'))
        for sequence in range(1, COUNT):
            line = json.dumps({'sequence': sequence, 'value': '测' + 'x' * 1998}, ensure_ascii=False)
            separator = '' if sequence == COUNT - 1 else '\r\n' if sequence % 2 == 0 else '\n'
            stream.write((line + separator).encode('utf8'))
            if sequence % 1000 == 0:
                stream.write(b'  \r\n')


def wait_for(path):
    deadline = time.monotonic() + 6
    while not path.exists():
        assert time.monotonic() < deadline, path
        time.sleep(.02)


def ordinary_history(api):
    route = api.prefix + '/events'
    dictionary = api.prefix + '/caseDict'
    created = api.call('GET', route)
    assert created['total'] == 1 and created['events'][0]['type'] == 'case.created'
    advanced = api.call('POST', dictionary + ':advance',
                        {'state': 'V', 'confirmed': True, 'expected_revision': 0})
    assert advanced['workflow_state'] == 'V' and advanced['dict_revision'] == 1
    content = api.call('GET', dictionary)['content'] + '\n// case timeline proof\n'
    saved = api.call('PUT', dictionary, {'content': content, 'expected_revision': 1})
    assert saved['dict_revision'] == 2
    raw_path = api.prefix + '/raw?path=' + PATH
    before = api.call('GET', raw_path, raw=True)
    api.call('PUT', dictionary, {'content': 'broken { {', 'expected_revision': 2}, expect=422)
    assert api.call('GET', raw_path, raw=True) == before
    events = [json.loads(line) for line in before.splitlines()]
    assert all(event['schema'] == 'vibeflow.case-event.v1' and event['case_id'] == api.case_id
               and event['event_id'] and event['created_at'] for event in events)
    assert [event['revision'] for event in events] == [0, 1, 2]
    assert [(event['source'], event['previous_state'], event['workflow_state']) for event in events] == [
        ('api', None, 'A'), ('workflow-advance', 'A', 'V'), ('api', 'V', 'V')]
    assert [event['type'] for event in events] == ['case.created', 'caseDict.updated', 'caseDict.updated']
    assert api.call('GET', f'{route}?limit=2')['events'] == list(reversed(events[1:]))
    assert api.call('GET', f'{route}?limit=2&offset=2')['events'] == events[:1]
    other = api.call('POST', '/auth/register', {
        'email': uuid.uuid4().hex + '@example.com', 'password': 'isolated-second-123', 'name': 'Other timeline'})
    api.call('GET', route, expect=404, extra_headers={'Authorization': 'Bearer ' + other['token']})
    return {'actual_rest_revisions': [0, 1, 2], 'rejected_write_status': 422, 'other_account_status': 404}


def exercise(api, store, report):
    api.create_case('Timeline capacity')
    report.update(ordinary_history(api))
    case = api.call('GET', api.prefix)
    route = api.prefix + '/events'
    key = case['storage_prefix'].rstrip('/') + '/' + PATH
    timeline = api.root / 'files' / Path(key)
    history = api.root / 'source-history.jsonl'
    make_history(history)
    size = history.stat().st_size
    assert size >= 39 * 1024 * 1024

    def replace(source):
        if store:
            with source.open('rb') as stream:
                store.client.put_object(Bucket=store.bucket, Key=key, Body=stream)
        else:
            shutil.copyfile(source, timeline)

    replace(history)
    filename = api.root / 'peak.txt'
    for offset, expected in [(0, list(range(COUNT - 1, COUNT - 8, -1))),
                             (15000, list(range(COUNT - 15001, COUNT - 15008, -1))),
                             (COUNT - 1, [0]), (10**28, [])]:
        result = api.call('GET', f'{route}?offset={offset}&limit=7')
        assert [event['sequence'] for event in result['events']] == expected
        assert result['total'] == COUNT and result['offset'] == offset and result['limit'] == 7
        assert result['has_more'] == (offset + len(expected) < COUNT)
        if offset == COUNT - 1:
            assert result['events'][0]['value'].endswith('中')
        peak = int(filename.read_text())
        assert peak < size // 4, (size, peak)
        report.setdefault('page_peaks', {})[str(offset)] = peak
    api.call('GET', f'{route}?limit=201', expect=400)
    (api.root / 'measurement-off').touch()

    small = api.root / 'replacement.jsonl'
    small.write_text('{"sequence":212,"value":"replaced"}\n', encoding='utf8')
    release = api.root / 'release'
    selected = api.root / 'selected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        (api.root / 'hold').touch()
        read = pool.submit(api.call, 'GET', route, expect=409)
        try:
            wait_for(selected)
            write = pool.submit(api.call, 'PUT', api.prefix + '/file',
                                {'path': 'Ref/parallel.txt', 'content': 'write during history read'})
            assert write.result(timeout=15)['ok'] is True and not read.done()
            replace(small)
        finally:
            release.touch()
        assert read.result(timeout=10)['detail']['code'] == 'file_changed_during_read'
        assert api.call('GET', route)['events'][0]['sequence'] == 212
        report.update(concurrent_same_case_write=True, changed_version_status=409)

        release.unlink()
        selected.unlink()
        (api.root / 'hold').touch()
        read = pool.submit(api.call, 'GET', route, expect=504)
        try:
            wait_for(selected)
            time.sleep(READ_TIMEOUT + .2)
            assert not read.done(), 'The response completed before the storage thread drained'
        finally:
            release.touch()
        assert read.result(timeout=10)['detail']['code'] == 'case_events_timeout'
        assert api.call('GET', route)['total'] == 1
        report.update(timeout_status=504, retry_after_drain=True)

    if store:
        store.client.delete_object(Bucket=store.bucket, Key=key)
    else:
        timeline.unlink()
    assert api.call('GET', route)['total'] == 0
    report.update(history_bytes=size, records=COUNT, absent_history_empty=True)


def verify(database, store):
    backend = 's3' if store else 'fs'
    api = IsolatedServer('case-event-' + backend, app='event_fixture:app', database_url=database)
    settings = store.settings() if store else {}
    settings['VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS'] = str(READ_TIMEOUT)
    (api.root / 'event_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' +
                                               FIXTURE, encoding='utf8')
    report = {'passed': False, 'backend': backend, 'database': 'postgresql' if database else 'sqlite',
              'model_calls': 0, 'mock_llm_calls': 0}
    print('CASE_EVENT_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            native = json.loads((api.root / 'api-birth.json').read_text(encoding='utf8'))
            try:
                exercise(api, store, report)
            finally:
                with httpx.Client(timeout=10, trust_env=False) as client:
                    try:
                        client.post(api.base + '/fixture/stop', json={'pid': native['pid']},
                                    headers={'Authorization': 'Bearer ' + api.token})
                    except (httpx.RemoteProtocolError, httpx.ReadError):
                        pass
                api.proc.wait(20)
                report['api_exit'] = probe(native)
                assert api.proc.returncode == 79 and report['api_exit']['state'] == 'exited'
        report['passed'] = True
    finally:
        (api.root / 'case-event-paging.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print('CASE_EVENT_OK ' + str(api.root), flush=True)


async def main(backend):
    use_s3 = backend == 's3'
    context = ObjectStore(executable=RUNTIME, volume_max=12, authenticated=True) if use_s3 else nullcontext()
    async with disposable_database(use_s3) as database:
        with context as store:
            await asyncio.to_thread(verify, database, store)
    if use_s3:
        assert context.proc is not None and context.proc.poll() is not None


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs', 's3'), required=True)
    args = parser.parse_args()
    enforce([Path(__file__)])
    asyncio.run(main(args.backend))
