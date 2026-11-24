"""Real private S3 chunk cleanup across an interrupted upload cancellation."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import httpx

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


FIXTURE = '''
from contextvars import ContextVar
import json
from pathlib import Path

from app import storage
from app.main import app
from app.services import s3_uploads, uploads

root = Path(__file__).parent
active = ContextVar('active_cancel_observation', default=None)
original = s3_uploads._conditional


def observe_list(params, **_):
    state = active.get()
    prefix = params.get('Prefix', '')
    if state is None or not prefix.endswith('/chunks/') or '/.vibeflow/uploads/' not in prefix:
        return
    state['pages'].append({'max_keys':params.get('MaxKeys'), 'closed_before':state['closed']})
    if state['mode'] == 'interrupt' and len(state['pages']) == 2:
        raise uploads.UploadError('isolated interruption between object pages', 503)


def observe_close(prefix, path, data, etag, metadata):
    result = original(prefix, path, data, etag, metadata)
    state = active.get()
    if state is not None and '/chunks/' in path and data == b'':
        state['closed'] += 1
    return result


storage.s3().meta.events.register('before-parameter-build.s3.ListObjectsV2', observe_list)
s3_uploads._conditional = observe_close


@app.middleware('http')
async def observe(request, call_next):
    mode = request.headers.get('X-Fixture-Cancel')
    if request.method != 'DELETE' or mode not in ('baseline', 'interrupt', 'retry'):
        return await call_next(request)
    state = {'mode':mode,'closed':0,'pages':[]}
    token = active.set(state)
    try:
        response = await call_next(request)
        state['status'] = response.status_code
        return response
    finally:
        active.reset(token)
        (root/(mode+'.json')).write_text(json.dumps(state), encoding='utf-8')
'''


def row(api, statement, *args):
    database = api.root/'test.sqlite'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as connection:
        return connection.execute(statement, args).fetchone()


def inventory(store, stem):
    page = store.raw_client.list_objects_v2(Bucket=store.bucket, Prefix=stem, MaxKeys=1000)
    assert not page['IsTruncated']
    return {item['Key']:item['Size'] for item in page.get('Contents', [])}


def upload(api, store):
    case = api.create_case('Upload cancellation pages')
    case_prefix, = row(api, 'SELECT s3_prefix FROM cases WHERE id=?', case['id'])
    email, = row(api, 'SELECT users.email FROM users JOIN workspaces ON workspaces.user_id=users.id '
                  'JOIN cases ON cases.workspace_id=workspaces.id WHERE cases.id=?', case['id'])
    init = api.call('POST', api.prefix+'/uploads', {'path':'Ref/cancelled.bin', 'size':8192})
    identifier = init['id']
    endpoint = api.prefix+'/uploads/'+identifier
    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(trust_env=False, timeout=20) as client:
        part = client.put(api.base+'/api/v1'+endpoint, params={'offset':0}, headers=headers, content=b'ordinary chunk')
        assert part.status_code == 200, part.text
    stem = f'{case_prefix}/.vibeflow/uploads/{identifier}/chunks/'
    for offset in range(100, 3500, 100):
        for kind in ('data','record.json'):
            store.raw_client.put_object(Bucket=store.bucket, Key=f'{stem}{offset:010d}/{kind}',
                                        Body=b'legacy unregistered chunk')
    api.put('Ref/control.txt', 'unrelated file')
    initial = inventory(store, stem)
    assert len(initial) == 70 and all(initial.values()), initial
    return identifier, endpoint, stem, initial, email


def observe_cancel(api, endpoint, mode, status):
    api.call('DELETE', endpoint, expect=status, extra_headers={'X-Fixture-Cancel':mode})
    return json.loads((api.root/(mode+'.json')).read_text(encoding='utf-8'))


def settled(api, store, identifier, endpoint, stem, initial):
    closed = inventory(store, stem)
    assert set(closed) == set(initial) and not any(closed.values()), closed
    assert row(api, 'SELECT state,remaining_bytes FROM upload_storage_reservations WHERE id=?', identifier) == ('cancelled',0)
    api.call('GET', endpoint, expect=404)
    api.call('GET', api.prefix+'/raw?path=Ref/cancelled.bin', expect=404)
    assert api.call('GET', api.prefix+'/raw?path=Ref/control.txt', raw=True) == b'unrelated file'


def run(baseline):
    api = IsolatedServer('upload-cancel-pages', app='upload_cancel_fixture:app')
    report = {'passed':False,'baseline':baseline,'api_port':api.port,'real_model_calls':0,'mock_llm_calls':0}
    evidence = api.root/'cancel-pages.json'
    print('UPLOAD_CANCEL_PAGES_EVIDENCE', api.root, flush=True)
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    fixture = api.root/'upload_cancel_fixture.py'
    try:
        with ObjectStore(executable=runtime, volume_max=8, authenticated=True) as store:
            report['store_receipt'] = str(store.root/'store.json')
            settings = store.settings()
            temporary = api.root/'native-temp'
            temporary.mkdir()
            settings.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
            fixture.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE, encoding='utf-8')
            with api:
                identifier, endpoint, stem, initial, email = upload(api, store)
                report['initial_chunks'] = len(initial)
                if baseline:
                    trace = report['baseline_cancel'] = observe_cancel(api, endpoint, 'baseline', 200)
                    assert len(trace['pages']) == 1 and trace['pages'][0]['closed_before'] == 0, trace
                    settled(api, store, identifier, endpoint, stem, initial)
                else:
                    trace = report['interrupted_cancel'] = observe_cancel(api, endpoint, 'interrupt', 503)
                    assert trace['pages'] == [{'max_keys':32,'closed_before':0},
                                             {'max_keys':32,'closed_before':32}], trace
                    assert trace['closed'] == 32
                    partial = inventory(store, stem)
                    assert list(partial) == list(initial) and list(partial.values()).count(0) == 32
                    meta = store.raw_client.get_object(Bucket=store.bucket, Key=stem.removesuffix('chunks/')+'meta.json')
                    with closing(meta['Body']) as body:
                        assert json.load(body)['status'] == 'cancelled'
                    reservation = row(api, 'SELECT state,remaining_bytes FROM upload_storage_reservations WHERE id=?', identifier)
                    assert reservation[0] == 'active' and reservation[1] > 0, reservation
                    report['before_restart'] = {'physical_closed':32, 'reservation':reservation}
            report['first_api_exit'] = api.proc.poll()
            assert report['first_api_exit'] is not None
            if not baseline:
                with api:
                    api.token = api.call('POST','/auth/login', {'email':email,'password':'isolated-test-123'})['token']
                    retry = report['retried_cancel'] = observe_cancel(api, endpoint, 'retry', 200)
                    assert retry['pages'] == [{'max_keys':32,'closed_before':0},
                                               {'max_keys':32,'closed_before':0},
                                               {'max_keys':32,'closed_before':32}], retry
                    assert retry['closed'] == 38
                    settled(api, store, identifier, endpoint, stem, initial)
                report['second_api_exit'] = api.proc.poll()
                assert report['second_api_exit'] is not None
        report['store_exit'] = store.proc.poll()
        assert report['store_exit'] == 0
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1200]}
        raise
    finally:
        fixture.unlink(missing_ok=True)
        evidence.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    print('UPLOAD_CANCEL_PAGES_PASS', evidence, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    run(parser.parse_args().baseline)
