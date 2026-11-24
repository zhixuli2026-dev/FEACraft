"""Actual historical WSL SyncTeX under node disk admission and cleanup."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from app.services.process_identity_native import observe, probe
from testsupport import IsolatedServer, SERVER


SOURCE = 'e10-synctex-zyfsou5e'

FIXTURE = r'''
import json
from pathlib import Path

from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.services import fs_directory, synctex_execution

root = Path(__file__).parent
settings = get_settings()
original_limit = settings.node_storage_limit_mb
trace = root/'node-trace.jsonl'
capacity = root/'files'/'.node-storage-budget'

def record(stage, directory):
    claim = [json.loads(path.read_text(encoding='utf8'))['bytes'] for path in capacity.glob('*.json')]
    files = {path.name:path.stat().st_size for path in directory.iterdir()} if directory.exists() else {}
    with trace.open('a',encoding='utf8') as output:
        output.write(json.dumps({'stage':stage,'bytes':claim,'files':files})+'\n')

run = synctex_execution.run
remove = fs_directory.remove_owned

def observed_run(profile):
    directory = Path(profile['root'])
    record('before_native',directory)
    try:
        return run(profile)
    finally:
        record('after_native',directory)

def observed_remove(path, *, root):
    if root.name != synctex_execution.DIRECTORY:
        return remove(path,root=root)
    record('before_cleanup',path)
    remove(path,root=root)
    record('after_cleanup',path)

synctex_execution.run = observed_run
fs_directory.remove_owned = observed_remove

@app.post('/api/v1/test/synctex-node-pressure')
async def pressure(body:dict,user=Depends(get_current_user)):
    if body['full']:
        # Real bytes in the isolated node root, visible to its normal inventory.
        (root/'files'/'pressure.bin').write_bytes(b'P'*(1024*1024))
        settings.node_storage_limit_mb = 1
    else:
        (root/'files'/'pressure.bin').unlink()
        settings.node_storage_limit_mb = original_limit
    return {'limited':body['full']}
'''


def poll(fetch, check, seconds=15):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        value = fetch()
        if check(value):
            return value
        time.sleep(.05)
    raise TimeoutError(f'SyncTeX capacity did not reach cleanup: {value}')


def captured(api):
    uri = (api.root/'test.sqlite').as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as db:
        rows = db.execute("SELECT id,phase,children_json,error FROM native_requests WHERE kind='synctex.query'").fetchall()
    return {rid:(phase,json.loads(children),error) for rid,phase,children,error in rows}


def clone(api,source):
    files = source/'files'
    for path in files.rglob('*'):
        attributes = getattr(path.lstat(),'st_file_attributes',0)
        assert not path.is_symlink() and not attributes&0x400 and path.resolve().is_relative_to(files), path
    shutil.copytree(files,api.root/'files')
    path = source/'test.sqlite'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as old:
        with closing(sqlite3.connect(api.root/'test.sqlite')) as target:
            old.backup(target)
    return path


def check_stop(rows, previous):
    [rid] = rows.keys()-previous.keys()
    phase, children, error = rows[rid]
    assert phase=='succeeded' and error is None
    [child] = children['children'].values()
    stop = child['stop']
    assert child['purpose']=='synctex_query' and child['phase']=='stopped'
    assert stop['verified'] and stop['scope_empty'] and stop['native_state']=='exited'
    assert probe(child['identity']['host'])['state']=='exited'
    return {'request_id':rid,'native_state':stop['native_state'],'stop_verified':True}


def verify(api,point,expected,baseline,report):
    uri = (api.root/'test.sqlite').as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as db:
        case, email = db.execute('SELECT c.id,u.email FROM cases c JOIN workspaces w ON c.workspace_id=w.id '
                                 'JOIN users u ON w.user_id=u.id WHERE c.id=1').fetchone()
    endpoint = f'/cases/{case}/synctex:query'
    def login(email,password):
        return api.call('POST','/auth/login',{'email':email,'password':password})['token']
    api.token = login(email,'isolated-test-123')
    original = captured(api)
    api.call('POST','/test/synctex-node-pressure',{'full':True})
    attempt = api.call('POST',endpoint,point,expect=200 if baseline else 409)
    local = api.root/'files'/'.vibeflow-native-queries'
    if baseline:
        assert attempt['line']==expected['line'] and attempt['publication_id']==expected['publication_id']
        report['low_capacity'] = {'status':200,**check_stop(captured(api),original)}
    else:
        assert attempt['detail']['code']=='node_storage_full' and captured(api)==original, attempt
        assert not local.exists() or not list(local.iterdir())
        outsider = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                           'name':'Isolated SyncTeX visitor','password':'isolated-test-123'})['token']
        api.call('POST',endpoint,point,expect=404,extra_headers={'Authorization':'Bearer '+outsider})
        api.call('POST','/test/synctex-node-pressure',{'full':False})
        viewer = login('latex-reader@example.com','isolated-latex-e03-123')
        answer = api.call('POST',endpoint,point,extra_headers={'Authorization':'Bearer '+viewer})
        assert all(answer[key]==expected[key] for key in ('file','line','publication_id','source_sha256','source_current_matches'))
        report['low_capacity'] = {'status':409,'code':'node_storage_full','native_requests_created':0}
        report['recovered'] = {'status':200,'historical_source':answer['file'],'line':answer['line'],
                               'viewer':True,**check_stop(captured(api),original)}

    trace = [json.loads(line) for line in (api.root/'node-trace.jsonl').read_text(encoding='utf8').splitlines()]
    assert [row['stage'] for row in trace]==['before_native','after_native','before_cleanup','after_cleanup'], trace
    assert len(trace[0]['files'])==3 and not trace[-1]['files']
    bound = sum(size for path,size in trace[0]['files'].items() if path!='settings.json')+(16*1024)
    assert all(row['bytes']==([] if baseline else [bound]) for row in trace), trace
    assert not list(local.iterdir())
    assert not list((api.root/'files'/'.node-storage-budget').glob('*.json'))
    report.update(trace=trace,node_claims_released=True,private_directory_empty=True)


def main():
    baseline = os.environ.get('VIBEFLOW_SYNCTEX_NODE_BASELINE')=='1'
    tmp = (SERVER.parents[1]/'tmp').resolve()
    source = tmp/SOURCE
    assert source.resolve()==source and source.is_relative_to(tmp)
    captured_e10 = json.loads((source/'synctex-result.json').read_text(encoding='utf8'))
    assert captured_e10['passed'] and captured_e10['model_calls']==0
    saved = captured_e10['browser']['responses'][0]
    api = IsolatedServer('synctex-node-capacity',app='synctex_capacity_fixture:app',access_log=True)
    report = {'passed':False,'baseline':baseline,'api':str(api.root),'model_calls':0,'mock_llm_calls':0}
    try:
        sqlite = clone(api,source)
        state = hashlib.sha256(sqlite.read_bytes()).hexdigest()
        env = {'TEMP':str(api.root),'TMP':str(api.root),'TMPDIR':str(api.root)}
        (api.root/'synctex_capacity_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE,
                                                            encoding='utf8')
        with api:
            birth = observe(api.proc.pid)
            verify(api,saved['point'],saved['result'],baseline,report)
        report['api_exit'] = poll(lambda:probe(birth),lambda value:value['state']=='exited')
        assert hashlib.sha256(sqlite.read_bytes()).hexdigest()==state
        report['passed']=True
    except BaseException as error:
        report.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        path = api.root/'synctex-node-capacity.json'
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
        print(f'SYNCTEX_NODE_CAPACITY_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
