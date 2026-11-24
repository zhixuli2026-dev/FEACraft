"""Measure actual failure-fallback history transfers on local and S3 storage."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from s3testsupport import ObjectStore
from testsupport import SERVER, IsolatedServer


BYTES = 40 * 1024 * 1024
DICTIONARY = '0-caseDict/caseDict'
TIMELINE = '0-caseDict/case-events.jsonl'
PROVENANCE = '0-caseDict/provenance.jsonl'
SETTLING = {'file_sync_busy', 'file_recovery_required', 'storage_quota_busy', 'validation_busy'}


def process_fixture():
    # Reuse the native fault launcher without importing the model readback.
    path = SERVER/'live_llm_failure_stage_policy_acceptance.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='FIXTURE'
                                              for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Native failure fixture is missing')


OBSERVER = '''
from contextlib import closing
import hashlib
from pathlib import Path
import time

from app import storage
from app.services.case_events import CASE_EVENTS_PATH
from app.services.file_read import FileRead

root = Path(__file__).parent
original = FileRead.chunks
armed = {}

def observe(reader, *args, **kwargs):
    prefix = armed.get('prefix')
    path = CASE_EVENTS_PATH
    selected = prefix and (reader.location['Key'] == storage._s3_key(prefix, path) if reader.remote
                           else reader.path == storage._fs_path(prefix, path))
    with closing(original(reader, *args, **kwargs)) as chunks:
        for chunk in chunks:
            if selected and reader.size == 40*1024*1024:
                label = armed['label']
                witness = root/(label+'-history.json')
                if not witness.exists():
                    pending = witness.with_suffix('.partial')
                    pending.write_text(json.dumps({'bytes':reader.size,'first_read':len(chunk)}),encoding='utf8')
                    pending.replace(witness)
                    limit = time.monotonic()+20
                    while not (root/(label+'-release')).exists():
                        if time.monotonic()>=limit:
                            raise TimeoutError('Independent writer did not finish while reading history')
                        time.sleep(.02)
            yield chunk

FileRead.chunks = observe

@app.post('/fixture/fallback-history/{case_id}')
async def seed(case_id:int,label:str,user=Depends(get_current_user),db=Depends(get_db)):
    assert label in {'A','V'}
    case = await get_owned_case(case_id,user,db)
    head, tail = b'{"data":"', b'"}'+bytes((10,))
    size = 40*1024*1024
    data = head+b'f'*(size-len(head)-len(tail))+tail
    storage.put_bytes(case.s3_prefix,CASE_EVENTS_PATH,data)
    armed.update(prefix=case.s3_prefix,label=label)
    return {'size':size,'sha256':hashlib.sha256(data).hexdigest()}
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def raw(api, prefix, path, *, expect=200):
    return api.call('GET',prefix+'/raw?path='+path,raw=True,expect=expect)


def read_settled(api, route, *, raw=False):
    """Read published content once its post-commit file journal has drained."""
    deadline = time.monotonic()+25
    while True:
        value = api.call('GET',route,raw=raw,expect=(200,409))
        detail = value
        if raw and isinstance(value, bytes) and value.startswith(b'{"detail":'):
            detail = json.loads(value)
        error = detail.get('detail') if isinstance(detail,dict) else None
        if not isinstance(error,dict):
            return value
        assert error.get('code') in SETTLING, error
        if time.monotonic()>=deadline:
            raise TimeoutError(f'{route} did not become readable after the fallback committed: {error}')
        time.sleep(.1)


def exercise(api, initial):
    case = api.create_case('Fallback '+initial)
    if initial=='V':
        api.advance('V')
    start = api.call('GET',api.prefix+'/caseDict')
    authored = raw(api,api.prefix,DICTIONARY)
    old_provenance = raw(api,api.prefix,PROVENANCE,expect=(200,404))
    workspace = api.call('GET','/workspaces')[0]['id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent writer'})
    other_prefix = f'/cases/{other["id"]}'
    api.put('Ref/stage-fault.py','print("REAL_FALLBACK_CAPACITY_EXIT",flush=True)\nraise SystemExit(29)\n')
    seed = api.call('POST',f'/fixture/fallback-history/{case["id"]}?label={initial}',api_prefix=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        submitted = executor.submit(api.call,'POST',api.prefix+'/testing/stage-failure',{'job_type':'solve'})
        try:
            witness = api.root/(initial+'-history.json')
            deadline = time.monotonic()+85
            while not witness.is_file():
                if submitted.done():
                    submitted.result()
                if time.monotonic()>deadline:
                    raise TimeoutError('Failed native Job did not reach actual history storage')
                time.sleep(.05)
            observed = json.loads(witness.read_text(encoding='utf8'))
            assert observed['bytes']==seed['size']==BYTES and 0<observed['first_read']<BYTES
            with sqlite3.connect(api.root/'test.sqlite',timeout=.3) as probe:
                probe.execute('BEGIN IMMEDIATE')
            data = {'path':'Ref/overlap.txt','content':'committed while the other case read its failure history'}
            api.call('PUT',other_prefix+'/file',data)
            assert raw(api,other_prefix,data['path'])==data['content'].encode()
        finally:
            (api.root/(initial+'-release')).touch()
        identifier = submitted.result(40)['job_id']
    job = api.wait_job(identifier,expected='failed',timeout=90)
    assert job['exit_code']==29 and job['attempt']['process_stop']['verified']
    assert 'REAL_FALLBACK_CAPACITY_EXIT' in job['log_backlog']
    route = f'/jobs/{identifier}/testing/stage-fallback'
    deadline = time.monotonic()+90
    while time.monotonic()<deadline:
        fallback = api.call('GET',route)
        if fallback.get('applied'):
            break
        time.sleep(.15)
    else:
        raise TimeoutError('Native failure did not yield its durable fallback receipt')
    assert fallback['actual_stop_and_drain_verified']
    regressed = initial=='V'
    expected = 'C' if regressed else 'A'
    assert fallback['stage']==expected and fallback['applied']['result']['regressed']==regressed
    current = read_settled(api,api.prefix+'/caseDict')
    revision = start['dict_revision']+int(regressed)
    assert current['dict_revision']==revision==fallback['applied']['revision']
    timeline = read_settled(api,api.prefix+'/raw?path='+TIMELINE,raw=True)
    assert digest(timeline[:BYTES])==seed['sha256']
    additions = [json.loads(line) for line in timeline[BYTES:].splitlines()]
    assert [event['type'] for event in additions]==(['caseDict.updated'] if regressed else [])+['workflow.fallback']
    assert additions[-1]['job_id']==identifier and additions[-1]['revision']==revision
    assert additions[-1]['workflow_state']==expected and additions[-1]['log_path']
    dictionary = raw(api,api.prefix,DICTIONARY)
    provenance = raw(api,api.prefix,PROVENANCE,expect=(200,404))
    if regressed:
        assert dictionary!=authored
        last = json.loads(provenance.splitlines()[-1])
        assert last['revision']==revision and last['casedict_sha256']==digest(dictionary)
    else:
        assert dictionary==authored and provenance==old_provenance
    replayed = api.call('POST',route,{})
    assert replayed['replayed'] and raw(api,api.prefix,TIMELINE)==timeline
    assert api.call('GET',api.prefix+'/caseDict')['dict_revision']==revision
    return {'case_id':case['id'],'job_id':identifier,'initial':initial,'result':expected,'exit_code':29,
            'history_bytes':BYTES,'first_read':observed['first_read'],'another_case_committed':True,
            'sqlite_writer_available':True,'process_stopped':True,'timeline_sha256':digest(timeline),
            'replay_preserved_files':True}


def run(backend):
    native = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    context = ObjectStore(executable=native,volume_max=10,authenticated=True) if backend=='s3' else nullcontext()
    with context as store:
        api = IsolatedServer('fallback-capacity-'+backend,app='fallback_capacity_fixture:app')
        path = api.root/'fallback_capacity_fixture.py'
        env = 'import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
        path.write_text(env+'from app.main import app\n'+process_fixture()+OBSERVER,encoding='utf8')
        try:
            with api:
                for state in ('A','V'):
                    yield {'backend':backend,'root':str(api.root),**exercise(api,state)}
        finally:
            path.unlink(missing_ok=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('fs','s3','all'),default='all')
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    evidence = {'passed':False,'actual_model_calls':0,'simulated_model_calls':0,'runs':[]}
    try:
        for backend in ('fs','s3') if args.backend=='all' else (args.backend,):
            for result in run(backend):
                evidence['runs'].append(result)
                print('CASEDICT_FALLBACK_CAPACITY_PASS',backend,result['initial'],flush=True)
        evidence['passed'] = True
    finally:
        args.report.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf8')
