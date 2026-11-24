"""Exercise real local file edits across a private Linux bind mount."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from testsupport import SERVER, IsolatedServer


SIZE = 40 * 1024 * 1024
FILE = 'Ref/cross-volume/mounted.bin'
NEW = 'cross-volume committed body'
FIXTURE = '''
import hashlib
import json
import os
from pathlib import Path
import time

from cryptography.fernet import Fernet
root = Path(__file__).parent
key = root/'.vault_key'
if not key.exists():
    key.write_bytes(Fernet.generate_key())
os.environ['VIBEFLOW_VAULT_KEY_PATH'] = str(key)

from app.main import app
from app.services import file_transactions, local_file_edits

original_capture = file_transactions._capture
original_publish = local_file_edits.PreparedEdit.publish_files
original_finish = file_transactions.finish_committed_edit

def exit_at(name, case, code):
    arm = root/name
    if arm.exists() and int(arm.read_text())==case.id:
        arm.unlink()
        os._exit(code)

def publish(edit, case, pending):
    result = original_publish(edit,case,pending)
    exit_at('exit-before-commit',case,84)
    return result

async def finish(db, case, owner_id):
    exit_at('exit-after-commit',case,85)
    return await original_finish(db,case,owner_id)

def capture(path, destination=None, *, stream=None, **options):
    request = root/'pause-copy.json'
    arm = json.loads(request.read_text()) if request.exists() else {}
    if arm.get('source')==str(path) and (not arm.get('copy_only') or destination is not None):
        size = path.stat().st_size
        if size==40*1024*1024 and not (root/'copy-started.json').exists():
            def transfer(target):
                class Body:
                    def write(self, block):
                        if target is not None:
                            target.write(block)
                        witness = root/'copy-started.json'
                        if not witness.exists():
                            ready = witness.with_suffix('.tmp')
                            ready.write_text(json.dumps({'size':size,'received':len(block)}),encoding='utf8')
                            ready.replace(witness)
                            deadline = time.monotonic()+20
                            while not (root/'release-copy').exists():
                                if time.monotonic()>=deadline:
                                    raise TimeoutError('Independent write did not finish')
                                time.sleep(.02)
                        return len(block)
                return original_capture(path, stream=Body(), **options)
            if destination is not None:
                destination.parent.mkdir(parents=True,exist_ok=True)
                with destination.open('xb') as output:
                    value = transfer(output)
                    output.flush()
                    os.fsync(output.fileno())
                    return value
            return transfer(stream)
    return original_capture(path,destination,stream=stream,**options)
file_transactions._capture = capture
local_file_edits.PreparedEdit.publish_files = publish
file_transactions.finish_committed_edit = finish
'''


@contextmanager
def mounted_at(target, data_root):
    target.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory(dir=data_root,prefix='mounted-data-') as data:
        subprocess.run(['/usr/bin/mount','--bind',data,str(target)],check=True)
        try:
            assert target.stat().st_dev != target.parent.stat().st_dev
            yield target.stat().st_dev
        finally:
            subprocess.run(['/usr/bin/umount',str(target)],check=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def seed(path):
    block = b'O'*(1024*1024)
    hasher = hashlib.sha256()
    with path.open('wb') as stream:
        for _ in range(SIZE//len(block)):
            stream.write(block)
            hasher.update(block)
    return hasher.hexdigest()


def wait(probe, description, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        value = probe()
        if value:
            return value
        time.sleep(.02)
    raise TimeoutError(description)


def concurrent_copy(api, other, name, source, request, *, expected_lock, copy_only=False, claim=None):
    witness = api.root/'copy-started.json'
    release = api.root/'release-copy'
    witness.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    marker = api.root/'pause-copy.json'
    marker.write_text(json.dumps({'source':str(source),'copy_only':copy_only}),encoding='utf8')
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(request)
        try:
            def progressed():
                if future.done():
                    raise AssertionError(('Request did not reach the monitored copy',future.result()))
                return witness.exists()
            wait(progressed,'Cross-volume copy did not start')
            result = json.loads(witness.read_text())
            assert result['size']==SIZE and 0<result['received']<SIZE
            if claim is not None:
                claim_root, target, minimum = claim
                rows = [json.loads(path.read_text()) for path in claim_root.glob('*.json')]
                claims = [row['bytes'] for row in rows if row.get('target_root')==str(target)]
                assert len(claims)==1 and claims[0]>=minimum,rows
                result['target_volume_reserved_bytes'] = claims[0]
            can_write = True
            try:
                with sqlite3.connect(api.root/'test.sqlite',timeout=.35) as db:
                    db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                assert 'locked' in str(error).lower(),error
                can_write = False
            result['sqlite_writer_available'] = can_write
            result['another_case_committed'] = False
            if can_write:
                other_route = f'/cases/{other}'
                path = f'Ref/independent-{name}.txt'
                api.call('PUT',other_route+'/file',{'path':path,'content':'concurrent'})
                assert api.call('GET',other_route+'/raw?path='+path,raw=True)==b'concurrent'
                result['another_case_committed'] = True
            assert can_write is expected_lock,result
        finally:
            release.touch()
        response = future.result(30)
    marker.unlink()
    (api.root/f'{name}-observation.json').write_text(json.dumps(result),encoding='utf8')
    return result, response


def restart_after_save(api, case_id, mode, code, body):
    (api.root/mode).write_text(str(case_id),encoding='utf8')
    try:
        api.put(FILE,body)
    except OSError:
        pass
    else:
        raise AssertionError('Expected a real API process exit')
    assert api.proc.wait(timeout=10)==code
    old_pid = api.proc.pid
    api.proc = subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT)

    def healthy():
        try:
            return api.call('GET','/api/health',api_prefix=False)['ok']
        except OSError:
            return False
    wait(healthy,'Fresh API did not become ready')
    return {'old_pid':old_pid,'new_pid':api.proc.pid,'exit_code':code}


def cold_recovery(api, case_id, other, case, path, claim_root):
    journal = case/'.vibeflow/file-transactions'
    pending = journal/'pending'
    scratch = path.parent/'.vibeflow-file-staging'
    before = seed(path)
    uncommitted = 'installed before SQL commit'
    rolled_back = restart_after_save(api,case_id,'exit-before-commit',84,uncommitted)
    assert path.read_bytes()==uncommitted.encode()
    assert any(scratch.glob('*/owner.json'))
    transaction_id = json.loads((pending/'manifest.json').read_text())['plan']['id']
    observed, _ = concurrent_copy(api,other,'rollback',pending/'before/0',
        lambda: api.call('GET',api.prefix+'/file:version?path='+FILE),expected_lock=True,copy_only=True,
        claim=(claim_root,scratch,SIZE))
    rolled_back.update(observed)
    final = api.call('GET',api.prefix+'/raw?path='+FILE,raw=True)
    assert len(final)==SIZE and digest(final)==before
    receipt = json.loads((journal/'history'/transaction_id/'resolution.json').read_text())
    assert receipt['decision']=='rolled_back' and not pending.exists()
    assert not list(scratch.iterdir())
    rolled_back.update(decision=receipt['decision'],sha256=before,staging_cleared=True)

    committed_body = 'installed and SQL committed'
    committed = restart_after_save(api,case_id,'exit-after-commit',85,committed_body)
    transaction_id = json.loads((pending/'manifest.json').read_text())['plan']['id']
    final = api.call('GET',api.prefix+'/raw?path='+FILE,raw=True)
    assert final==committed_body.encode()
    receipt = json.loads((journal/'history'/transaction_id/'resolution.json').read_text())
    assert receipt['decision']=='committed' and not pending.exists()
    assert not list(scratch.iterdir())
    committed.update(decision=receipt['decision'],sha256=digest(final),staging_cleared=True)
    return {'rollback':rolled_back,'committed':committed}


def run(api, storage_root, observe_lock):
    with api:
        api.create_case('Cross-volume file staging')
        row = api.call('GET',api.prefix)
        case = (storage_root/row['storage_prefix']).resolve()
        mount = case/Path(FILE).parent
        workspace = api.call('GET','/workspaces')[0]['id']
        other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent writer'})['id']
        with mounted_at(mount,Path('/dev/shm')) as device:
            path = case/FILE
            assert path.resolve().is_relative_to(case)
            before = seed(path)
            claim_root = storage_root/'.node-storage-budget'
            scratch = path.parent/'.vibeflow-file-staging'
            claim = None if observe_lock else (claim_root,scratch,len(NEW.encode()))
            result, _ = concurrent_copy(api,other,'write',path,lambda: api.put(FILE,NEW),
                                        expected_lock=not observe_lock,claim=claim)
            final = api.call('GET',api.prefix+'/raw?path='+FILE,raw=True)
            assert final==NEW.encode()
            base = case/'.vibeflow/file-transactions/history'
            retained = [candidate for candidate in base.glob('*/before/*') if candidate.stat().st_size==SIZE]
            original, = retained
            assert digest(original.read_bytes())==before
            result.update(root=str(api.root),case_device=case.stat().st_dev,file_device=device,
                          previous_sha256=before,current_sha256=digest(final),journal_preserved=True)
            if not observe_lock:
                tree = api.call('GET',api.prefix+'/tree')['entries']
                assert not any('.vibeflow-file-staging' in entry['path'] for entry in tree)
                private_path = str(Path(FILE).parent/'.vibeflow-file-staging'/'owner.json')
                api.call('GET',api.prefix+'/raw?path='+private_path,expect=422)
                result['staging_private'] = True
                result['cold_recovery'] = cold_recovery(api,row['id'],other,case,path,claim_root)
                assert not list(claim_root.glob('*.json'))
                result['node_claims_cleared'] = True
            return result


def verify(observe_lock):
    api = IsolatedServer('cross-volume-file',app='cross_volume_fixture:app')
    with tempfile.TemporaryDirectory(dir='/tmp',prefix='vibeflow-cross-volume-') as native:
        storage_root=Path(native)
        override=f'import os\nos.environ["VIBEFLOW_STORAGE_FS_ROOT"] = {str(storage_root)!r}\n'
        (api.root/'cross_volume_fixture.py').write_text(override+FIXTURE,encoding='utf8')
        return run(api,storage_root,observe_lock)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-lock',action='store_true')
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    enforce([Path(__file__)])
    evidence=verify(args.observe_lock)
    args.report.write_text(json.dumps({'passed':True,'actual_model_calls':0,'simulated_model_calls':0,
                                      'write':evidence},indent=2)+'\n',encoding='utf8')
    print('REAL_CROSS_VOLUME_FILE_PASS',args.report,flush=True)
