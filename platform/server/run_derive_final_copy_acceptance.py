"""Real Windows filesystem copy, source revalidation, and request timeout."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import httpx

from run_derive_source_lock_smoke import BASE, TARGET, CONTROL, EDIT, SIZE, absent, seed
from case_copy_source_lock_smoke import digest_response, read_observation, writer_available
from testsupport import IsolatedServer, SERVER


FIXTURE = r'''
import os
import time
import json
from contextvars import ContextVar
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import run_derive, node_storage_budget, process_identity_native

root = Path(__file__).parent
phase = ContextVar('observed_derive_phase', default=None)
copying = ContextVar('observed_derive_copy', default=False)
original_prepare = run_derive.DerivedRun.prepare
original_inventory = run_derive.source_files
original_open = Path.open

def mark(name, **values):
    evidence = root / (name + '.json')
    pending = evidence.with_suffix('.new')
    pending.write_text(json.dumps(values), encoding='utf8')
    pending.replace(evidence)
    deadline = time.monotonic() + 25
    while not (root / ('release-' + name)).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Isolated copy observation was not released')
        time.sleep(.02)

class ObservedBody:
    def __init__(self, stream):
        self.stream = stream
        self.observed = False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def read(self, size):
        data = self.stream.read(size)
        if data and not self.observed:
            self.observed = True
            mark(phase.get(), block_bytes=len(data))
        return data

def observe_open(self, *args, **kwargs):
    opened = original_open(self, *args, **kwargs)
    if (copying.get() and phase.get() in {'copy', 'timeout'} and args == ('rb',)
            and self.as_posix().endswith('/C-run/derive-source/0/source-field.bin')):
        return ObservedBody(opened)
    return opened

def observe_source_inventory(*args, **kwargs):
    # A second full read may open the same source while prepare is still active.
    marker = copying.set(False)
    try:
        return original_inventory(*args, **kwargs)
    finally:
        copying.reset(marker)

def observe_prepare(self):
    marker = copying.set(True)
    try:
        result = original_prepare(self)
        if phase.get() == 'recheck':
            mark('recheck', private_target_exists=self.stage.is_dir())
        return result
    finally:
        copying.reset(marker)

Path.open = observe_open
run_derive.source_files = observe_source_inventory
run_derive.DerivedRun.prepare = observe_prepare

@app.get('/fixture/derive-final/{case_id}')
async def fixture(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return {'prefix': case.s3_prefix, 'pid': process_identity_native.observe(os.getpid()),
            'node_claimed_bytes': node_storage_budget.observe()['reserved_bytes']}

class ObserveRequest:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        requested = dict(scope.get('headers', ())).get(b'x-fixture-derive-final')
        if not scope.get('path','').endswith('/runs:derive') or requested is None:
            return await self.app(scope, receive, send)
        selected = requested.decode('ascii')
        marker = phase.set(selected)
        config = get_settings()
        if selected == 'timeout':
            original_limit = config.run_derive_timeout_seconds
            config.run_derive_timeout_seconds = 2
        try:
            return await self.app(scope, receive, send)
        finally:
            phase.reset(marker)
            if selected == 'timeout':
                config.run_derive_timeout_seconds = original_limit

app = ObserveRequest(app)
'''


def verify(baseline=False):
    api = IsolatedServer('run-derive-final-fs', app='derive_final_fixture:app')
    (api.root/'derive_final_fixture.py').write_text(FIXTURE,encoding='utf8')
    report = {'passed':False, 'baseline':baseline, 'backend':'fs', 'actual_model_calls':0,
              'mock_llm_calls':0, 'api_port':api.port, 'checks':[]}
    print('DERIVE_FINAL_EVIDENCE '+str(api.root),flush=True)

    def save():
        (api.root/'derive-final.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

    try:
        with api:
            case = api.create_case('Run derivation final copy')
            fixture_path = '/fixture/derive-final/'+str(case['id'])
            bound = api.call('GET', fixture_path, api_prefix=False)
            prefix = bound['prefix']
            report['api_process'] = bound['pid']
            api.put(BASE+'/system/controlDict', CONTROL)
            previous, updated = 'staged value A', 'staged value B'
            api.put(EDIT, previous)
            report['source'] = seed(api, None, prefix)
            endpoint = api.prefix+'/runs:derive'
            command = {'base':BASE, 'name':TARGET.rsplit('/',1)[1]}
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                              trust_env=False, timeout=70) as client:
                def issue(worker, mode):
                    return worker.submit(client.post, endpoint, json=command, headers={'X-Fixture-Derive-Final':mode})

                with ThreadPoolExecutor(max_workers=1) as worker:
                    pending = issue(worker, 'copy')
                    try:
                        first = read_observation(api,'copy.json')
                        assert 0 < first['block_bytes'] <= 1024*1024 and not pending.done()
                        report['writer_available_during_copy'] = writer_available(api)
                        edit = client.put(api.prefix+'/file', json={'path':EDIT, 'content':updated,
                            'expected_sha256':hashlib.sha256(previous.encode()).hexdigest()},timeout=15)
                    finally:
                        (api.root/'release-copy').touch()
                    copied = pending.result(timeout=50)
                report['checks'].append({'name':'same_case_save_during_copy', 'save':edit.status_code,
                    'derive':copied.status_code, 'block_bytes':first['block_bytes']})
                save()
                assert report['writer_available_during_copy'] is (not baseline)
                if baseline:
                    assert edit.status_code==409 and copied.status_code==200, (edit.text,copied.text)
                    assert client.get(api.prefix+'/raw',params={'path':TARGET+'/0/initial-note.txt'}).text==previous
                else:
                    assert edit.status_code==200 and copied.status_code==409, (edit.text,copied.text)
                    report['stale_copy'] = absent(api,client,None,prefix)

                    with ThreadPoolExecutor(max_workers=1) as worker:
                        pending = issue(worker, 'recheck')
                        try:
                            assert read_observation(api,'recheck.json')['private_target_exists']
                            source = (api.root/'files'/prefix/EDIT).resolve()
                            assert source.is_relative_to(api.root.resolve()) and source.read_text()==updated
                            original = source.stat()
                            source.write_text(previous,encoding='utf8')
                            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
                            revised = source.stat()
                            assert (revised.st_size, revised.st_mtime_ns)==(original.st_size, original.st_mtime_ns)
                        finally:
                            (api.root/'release-recheck').touch()
                        changed = pending.result(timeout=50)
                    assert changed.status_code==409, changed.text
                    report['checks'].append({'name':'source_change_with_restored_mtime_refused',
                                             'derive':changed.status_code})
                    report['raw_change'] = absent(api,client,None,prefix)

                    with ThreadPoolExecutor(max_workers=1) as worker:
                        pending = issue(worker, 'timeout')
                        try:
                            assert read_observation(api,'timeout.json')['block_bytes'] <= 1024*1024
                            time.sleep(2.2)
                            assert not pending.done(), 'HTTP timeout returned before the owned reader drained'
                        finally:
                            (api.root/'release-timeout').touch()
                        timed_out = pending.result(timeout=50)
                    assert timed_out.status_code==504 and timed_out.json()['detail']['code']=='run_derive_timeout', timed_out.text
                    report['checks'].append({'name':'timeout_waits_for_owned_copy', 'http':timed_out.status_code})
                    report['timeout'] = absent(api,client,None,prefix)

                    result = client.post(endpoint,json=command)
                    assert result.status_code==200 and result.json()['files']==3, result.text
                    assert client.get(api.prefix+'/raw',params={'path':TARGET+'/0/initial-note.txt'}).text==previous
                    assert client.post(endpoint,json=command).status_code==409
                    report['copy'] = digest_response(client,case['id'],TARGET+'/0/source-field.bin')
                    assert report['copy']==report['source'] and report['copy']['size']==SIZE
                    report['checks'].append({'name':'same_name_retry_and_no_overwrite', 'http':result.status_code})
                    base = 'C-run/derive-mixed-case'
                    api.put(base+'/System/CONTROLDICT',CONTROL)
                    mixed = client.post(endpoint,json={'base':base, 'name':'uppercase-control-copy'})
                    assert mixed.status_code==200 and mixed.json()['files']==1, mixed.text
                    source = api.root/'files'/prefix/'C-run/uppercase-control-copy'
                    assert [item.relative_to(source).as_posix() for item in source.rglob('*') if item.is_file()]==['System/CONTROLDICT']
                    report['checks'].append({'name':'windows_existing_mixed_case_control_path', 'http':mixed.status_code})
            report['node_claimed_after'] = api.call('GET',fixture_path,api_prefix=False)['node_claimed_bytes']
            assert report['node_claimed_after']==0
            temporary = api.root/'files'/'.derive-staging'
            assert not temporary.exists() or not any(temporary.iterdir())
        assert api.proc.poll() is not None
        report['api_exited']=True
        report['passed']=True
    finally:
        save()


if __name__ == '__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    verify(parser.parse_args().baseline)
