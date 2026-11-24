"""Recover the two observed dead-owner RAW reservations via actual admission."""

from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

import httpx

from app.services.process_identity_native import observe, probe
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


SOURCES = {False:'manual-restore-plan-u20m8tnl', True:'manual-restore-plan-39g7ycic'}
S3_SOURCE = 's3-attempt-store-t955zmvw'
FS_FIRST_BACKUP = 'raw-download-claim-recovery-ccvjlsvf'
CLAIMS = '.node-storage-budget'
TARGET = 'Ref/restored-large.bin'
SIZE = 40 * 1024 * 1024
SHA = 'c24fd321f34ab8e4d48b8b91fa22d2243bb0d821d4dd6949f15740f147b9c7e8'

FIXTURE = r'''
from contextvars import ContextVar
import json
from pathlib import Path
import traceback
from app.services import node_storage_budget

root = Path(__file__).parent
phase = ContextVar('observed_node_capacity_phase',default='startup_or_http')
claims = node_storage_budget._claims

def observed_claims(fs, parent, counter):
    previous = set(parent.glob('*.json'))
    try:
        return claims(fs,parent,counter)
    finally:
        removed = sorted(path.name for path in previous if not path.exists())
        if removed:
            chain = [{'file':Path(frame.filename).name,'function':frame.name}
                     for frame in traceback.extract_stack(limit=18)]
            with (root/'claim-recovery-trace.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'phase':phase.get(),'removed':removed,'chain':chain})+'\n')

node_storage_budget._claims = observed_claims

from app.main import app
from app.services import storage_maintenance

accounts = storage_maintenance.maintain_accounts

async def account_pass(stop):
    token = phase.set('background_account_maintenance')
    try:
        value = await accounts(stop)
        (root/'accounts-completed').touch()
        return value
    finally:
        phase.reset(token)

storage_maintenance.maintain_accounts = account_pass
'''


def poll(read, check, seconds=60):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        value = read()
        if check(value):
            return value
        time.sleep(.05)
    raise TimeoutError(f'Isolated claim recovery did not reach its boundary: {value}')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            result.update(block)
    return result.hexdigest()


def inventory(root):
    found = {}
    for path in root.rglob('*'):
        attrs = path.lstat()
        if path.is_symlink() or getattr(attrs,'st_file_attributes',0)&0x400 or not path.resolve().is_relative_to(root):
            raise ValueError(f'Unexpected redirected fixture entry: {path}')
        if path.is_file():
            found[path.relative_to(root).as_posix()] = digest(path)
        elif not path.is_dir():
            raise ValueError(f'Unexpected non-ordinary fixture entry: {path}')
    return found


def copy_database(original, target):
    with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as origin:
        with closing(sqlite3.connect(target)) as copy:
            origin.backup(copy)
    with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as db:
        row = db.execute('SELECT c.id,u.email FROM cases c JOIN workspaces w ON c.workspace_id=w.id '
                         'JOIN users u ON w.user_id=u.id ORDER BY c.id').fetchall()
    assert len(row)==1, row
    return row[0]


def safe_source(tmp, name):
    root = tmp/name
    if root.resolve()!=root or not root.resolve().is_relative_to(tmp) or not root.is_dir():
        raise ValueError('Expected an exact prior disposable fixture root')
    return root


def raw_download(api, case_id):
    checksum = hashlib.sha256()
    size = 0
    with httpx.Client(timeout=110,trust_env=False) as client:
        with client.stream('GET',api.base+f'/api/v1/cases/{case_id}/raw',params={'path':TARGET},
                           headers={'Authorization':'Bearer '+api.token}) as response:
            assert response.status_code==200, response.status_code
            old = int(response.headers['Content-Length'])
            for block in response.iter_bytes(65536):
                size+=len(block)
                checksum.update(block)
    assert size==old==SIZE and checksum.hexdigest()==SHA
    return {'status':200,'bytes':size,'sha256':checksum.hexdigest()}


def run(api, origin, store, report):
    fs = origin/'files'
    assert fs.resolve()==fs
    prior = json.loads((origin/'post-exit-resource-observation.json').read_text(encoding='utf8'))
    [evidence] = prior['dead_owner_node_claims']
    claim = fs/CLAIMS/evidence['recorded_file']
    restored_from_first = not claim.exists() and store is None
    original_claim = claim
    if restored_from_first:
        first = safe_source(origin.parent,FS_FIRST_BACKUP)
        original_claim = first/'original-files'/CLAIMS/claim.name
        captured = json.loads((first/'raw-download-claim-recovery.json').read_text(encoding='utf8'))
        assert digest(original_claim)==captured['original']['claim_sha256']
    claim_data = original_claim.read_bytes()
    row = json.loads(claim_data)
    assert row['schema']=='vibeflow.node-storage-claim.v1' and row['id']==claim.stem
    assert row['root']==str(fs) and row['bytes']==SIZE and row['owner']['pid']==evidence['owner_pid']
    assert probe(row['owner'])==evidence['owner_exit']=={'state':'exited','reason':'process_exited'}
    if restored_from_first:
        # Replay the exact original bytes at their still-bound isolated path.
        # The original first-run evidence remains read-only.
        with claim.open('xb') as stream:
            stream.write(claim_data)
    source_db = origin/'test.sqlite'
    db_sha = digest(source_db)
    case_id, email = copy_database(source_db,api.root/'test.sqlite')

    original_inventory = inventory(fs)
    archive = api.root/'original-files'
    shutil.copytree(fs,archive)
    assert inventory(archive)==original_inventory
    (api.root/'original-files-sha256.json').write_text(json.dumps(original_inventory,indent=2)+'\n',encoding='utf8')
    key = claim.relative_to(fs).as_posix()
    claim_sha = original_inventory[key]
    report['original'] = {'root':str(origin),'bytes':row['bytes'],'owner_exit':evidence['owner_exit'],
        'claim_sha256':claim_sha,'evidence_backup':str(archive),'sqlite_snapshot':True,
        'exact_bytes_replayed':restored_from_first}

    def claim_exists():
        try:
            current = digest(claim)
        except FileNotFoundError:
            return False
        assert current==claim_sha
        return True

    env = {'VIBEFLOW_STORAGE_FS_ROOT':str(fs),'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED':'true',
           'TEMP':str(api.root),'TMP':str(api.root),'TMPDIR':str(api.root)}
    if store is not None:
        env.update(store.settings())
    (api.root/'raw_claim_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE,
                                                encoding='utf8')
    with api:
        birth = observe(api.proc.pid)
        after_health = claim_exists()
        poll((api.root/'accounts-completed').exists,bool)
        after_background = claim_exists()
        api.token = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        maintenance = api.call('POST','/case-provisions:maintenance')
        assert maintenance['case_scan']['through_case_id']==case_id
        report['existing_maintenance'] = {'started':True,'background_accounts_completed':True,
            'public_status':200,'claim_at_api_health':after_health,'claim_after_background':after_background,
            'claim_after_public_maintenance':claim_exists()}
        report['download'] = raw_download(api,case_id)
        remaining = poll(lambda:sorted(item.name for item in (fs/CLAIMS).glob('*.json')),
                         lambda files:not files,20)
        assert not remaining
        report['new_download_drained'] = True
    report['api_exit'] = poll(lambda:probe(birth),lambda result:result['state']=='exited',12)

    trace = (api.root/'claim-recovery-trace.jsonl').read_text(encoding='utf8').splitlines()
    [recovery] = [item for item in map(json.loads,trace) if claim.name in item['removed']]
    assert any(frame['file']=='node_storage_budget.py' and frame['function']=='_capacity' for frame in recovery['chain'])
    report['claim_recovery'] = recovery

    assert digest(archive/key)==claim_sha and digest(source_db)==db_sha
    original_inventory.pop(key)
    current_inventory = inventory(fs)
    assert current_inventory==original_inventory, sorted(set(current_inventory)^set(original_inventory))
    report['host_artifacts'] = {'old_claim_removed':not claim.exists(),'source_sqlite_unchanged':True,
                                'other_original_files_unchanged':True,'original_claim_preserved_in_backup':True}


def main():
    remote = os.environ.get('VIBEFLOW_RAW_CLAIM_S3')=='1'
    tmp = (SERVER.parents[1]/'tmp').resolve()
    origin = safe_source(tmp,SOURCES[remote])
    if remote:
        remote_source = safe_source(tmp,S3_SOURCE)
        store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                            volume_max=16,authenticated=True,restore_from=remote_source)
    else:
        store = None
    api = IsolatedServer('raw-download-claim-recovery',app='raw_claim_fixture:app',access_log=True)
    report = {'passed':False,'backend':'SeaweedFS' if remote else 'fs','api':str(api.root),
              'model_calls':0,'mock_llm_calls':0}
    try:
        if store is not None:
            report['object_store'] = str(store.root)
        with store if store is not None else nullcontext():
            run(api,origin,store,report)
        if store is not None:
            assert store.proc.poll() is not None
            unchanged = json.loads((store.root/'restore-final.json').read_text(encoding='utf8'))
            assert unchanged=={'original_unchanged':True},unchanged
            report['store_exit'] = {'isolated_launcher_exited':True,'original_source_unchanged':True}
        report['passed'] = True
    except BaseException as error:
        report.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        path = api.root/'raw-download-claim-recovery.json'
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
        print(f'RAW_DOWNLOAD_CLAIM_RECOVERY_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
