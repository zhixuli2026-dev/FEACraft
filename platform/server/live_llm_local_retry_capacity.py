"""Observe real filesystem retry copies, rollback and worker execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
import sqlite3
import sys
import time

import httpx

from live_llm_job_repeat_acceptance import main, put, write
from live_llm_job_retry_capacity import SOURCE, wait_for


FIXTURE = '''
import json
import os
from pathlib import Path
import time
from app.services import attempt_workspace

def hold(folder,identifier):
    marker=root/'local-retry-entered.json'
    if (root/'local-retry-arm').is_file() and not marker.exists():
        copies=sorted(path.relative_to(root/'files').as_posix() for path in folder.rglob('retry-source.bin') if path.is_file())
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'copies':copies,'attempt_id':identifier,'pid':os.getpid()}),encoding='utf8')
        temporary.replace(marker)
        deadline=time.monotonic()+35
        while not (root/'local-retry-release').is_file():
            if time.monotonic()>deadline:
                raise TimeoutError('Actual filesystem retry did not drain')
            time.sleep(.025)

if (root/'local-retry-baseline').is_file():
    actual=attempt_workspace.Snapshot.prepare
    def prepare(self):
        result=actual(self)
        hold(self.target,result['attempt_id'])
        return result
    attempt_workspace.Snapshot.prepare=prepare
else:
    from app.services import local_retry_seal
    actual=local_retry_seal.prepare_files
    def prepare(*args):
        result=actual(*args)
        if not (root/'local-retry-install-crash').is_file():
            hold(result.directory,result.id)
        return result
    local_retry_seal.prepare_files=prepare
    install=local_retry_seal.PreparedRetry.install
    def installed(self,*args):
        result=install(self,*args)
        if (root/'local-retry-install-crash').is_file():
            hold(result[0],self.id)
        return result
    local_retry_seal.PreparedRetry.install=installed
'''


def free_plan(api, update=None):
    assert api.database_url is None
    with sqlite3.connect(api.root/'test.sqlite',timeout=5) as db:
        current=db.execute("SELECT quotas FROM plans WHERE code='free'").fetchone()[0]
        if update is not None:
            assert db.execute("UPDATE plans SET quotas=? WHERE code='free'",(update,)).rowcount==1
    return current


def scenario(api, *, baseline, fault, **_):
    source=b'original filesystem input\n'+b'l'*(12*1024*1024)
    signature=hashlib.sha256(source).hexdigest()
    gate=api.root/'retry-fs-gate'
    proof=api.root/'retry-fs-native-proof'
    code=("from pathlib import Path\nimport hashlib\n"
          "with Path('Ref/retry-source.bin').open('rb') as f: sha=hashlib.file_digest(f,'sha256').hexdigest()\n"
          "print(sha,flush=True)\n"
          f"if not Path({str(gate)!r}).is_file(): raise SystemExit(22)\n"
          f"with Path({str(proof)!r}).open('a',encoding='utf8') as f: f.write(sha+'\\n')\n")
    put(api,'Ref/retry-capacity.py',code)
    case=api.call('GET',api.prefix)
    other=api.call('POST',f"/workspaces/{case['workspace_id']}/cases",{'name':'Independent local retry writer'})
    base=api.base+'/api/v1'
    key='local-filesystem-retry'
    marker=api.root/'local-retry-entered.json'
    released=api.root/'local-retry-release'
    sqlite_write=None
    separate_case=False
    rollback=None
    crashes=fault in {'staging','install'}
    with httpx.Client(headers={'Authorization':'Bearer '+api.token},timeout=120,trust_env=False) as client:
        uploaded=client.post(base+api.prefix+'/upload',params={'path':SOURCE},
                             files={'file':('old.bin',source,'application/octet-stream')})
        assert uploaded.status_code==200,uploaded.text[:400]
        first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/retry-capacity.py',
                       'input_paths':['Ref/retry-capacity.py',SOURCE]})
        old=api.wait_job(first['id'],expected='failed')
        assert old['exit_code']==22 and old['attempt']['process_stop']['verified'] and signature in old['log_backlog']
        put(api,SOURCE,'new current input\n')
        write(api,'PUT',api.prefix+'/caseDict/block',{'block':'report','entries':{'css':'local-retry-evidence.css'}})
        option=api.call('GET',f"/jobs/{first['id']}/operations")
        assert option['retry']['available']
        gate.touch()
        jobs={item['id'] for item in api.call('GET',api.prefix+'/jobs')}
        request={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
        route=base+f"/jobs/{first['id']}:repeat"
        if fault=='install':
            (api.root/'local-retry-install-crash').touch()
        (api.root/'local-retry-arm').touch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(client.post,route,json=request,headers={'Idempotency-Key':key})
            try:
                wait_for(marker,pending)
                measurement=json.loads(marker.read_text(encoding='utf8'))
                assert len(measurement['copies'])==2,measurement
                under_write=baseline or fault=='install'
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_write=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            sqlite_write=False
                        db.rollback()
                    assert sqlite_write is not under_write,sqlite_write
                if not under_write:
                    saved=client.put(base+f"/cases/{other['id']}/file",json={'path':'Ref/parallel.txt','content':'saved\n'},timeout=15)
                    assert saved.status_code==200 and not pending.done(),saved.text[:400]
                    separate_case=True
                def is_staged(path):
                    return '/.vibeflow/attempts/' not in '/'+path
                assert all(is_staged(path) is not under_write for path in measurement['copies'])
                if fault=='replace-source':
                    root=(api.root/'files'/case['storage_prefix']).resolve(strict=True)
                    archive=(root/old['attempt']['workspace']['inputs_path']/SOURCE).resolve(strict=True)
                    assert root.is_relative_to(api.root.resolve()) and archive.is_relative_to(root)
                    times=archive.stat()
                    archive.write_bytes(b'x'+source[1:])
                    os.utime(archive,ns=(times.st_atime_ns,times.st_mtime_ns))
                    changed=archive.stat()
                    assert changed.st_size==times.st_size and changed.st_mtime_ns==times.st_mtime_ns
                if fault=='quota-change':
                    original_plan=free_plan(api)
                    root=api.root/'files'/'workspaces'/'1'
                    used=sum(path.stat().st_size for path in root.rglob('*') if path.is_file())
                    limit_mb=(used+1024*1024-1)//(1024*1024)+2
                    assert used<limit_mb*1024*1024<used+len(source)*2
                    quotas={**json.loads(original_plan),'storage_mb':limit_mb}
                    free_plan(api,json.dumps(quotas))
                if crashes:
                    from live_llm_failure_fallback_acceptance import kill_owned_api
                    kill_owned_api(api,measurement['pid'])
            finally:
                released.touch()
            if crashes:
                try:
                    pending.result(timeout=25)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('API exited before committing the isolated retry')
            else:
                response=pending.result(timeout=120)
        if fault=='replace-source':
            try:
                assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:700]
            finally:
                archive.write_bytes(source)
                os.utime(archive,ns=(times.st_atime_ns,times.st_mtime_ns))
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==jobs
            assert all(not (api.root/'files'/path).exists() for path in measurement['copies'])
            response=client.post(route,json=request,headers={'Idempotency-Key':key})
            rollback={'boundary':fault,'job_absent':True,'copies_removed':True,'same_key_retried':True,
                      'seal_rollback_receipt':False,'old_mtime_preserved':True}
        if fault=='quota-change':
            try:
                assert response.status_code==429 and '存储空间' in response.json()['detail'],response.text[:700]
            finally:
                free_plan(api,original_plan)
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==jobs
            assert all(not (api.root/'files'/path).exists() for path in measurement['copies'])
            response=client.post(route,json=request,headers={'Idempotency-Key':key})
            rollback={'boundary':fault,'job_absent':True,'copies_removed':True,'same_key_retried':True,
                      'seal_rollback_receipt':False,'physical_bytes_before_commit':used,'reduced_limit_mb':limit_mb}
        if crashes:
            from live_llm_outbox_restart_acceptance import restart
            restart(api)
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==jobs
            response=client.post(route,json=request,headers={'Idempotency-Key':key})
            root=api.root/'files'/case['storage_prefix']
            attempt=measurement['attempt_id']
            assert not (root/'.vibeflow/attempts'/attempt).exists()
            assert all(not (api.root/'files'/path).exists() for path in measurement['copies'])
            history=root/'.vibeflow/attempt-provisions/history'/(attempt+'.json')
            if fault=='install':
                assert json.loads(history.read_text('utf8'))['outcome']=='rolled_back'
            else:
                assert not history.exists()
            rollback={'boundary':fault,'job_absent':True,'copies_removed':True,'same_key_retried':True,
                      'seal_rollback_receipt':history.exists()}
        assert response.status_code==202,response.text[:800]
        submitted=response.json()
        done=api.wait_job(submitted['id'],timeout=240)
        assert done['attempt']['retry_of_attempt_id']==option['source_attempt_id']
        assert done['attempt']['workspace']['files']==old['attempt']['workspace']['files']
        assert done['attempt']['workspace']['source']['dict_revision']==old['attempt']['workspace']['source']['dict_revision']
        assert signature in done['log_backlog'] and proof.read_text(encoding='utf8').splitlines()==[signature]
        repeated=client.post(route,json=request,headers={'Idempotency-Key':key})
        assert repeated.status_code==202 and repeated.json()['id']==submitted['id'] and not repeated.json()['created']
    report={'passed':True,'baseline':baseline,'fixture_bytes':len(source),'measurement':measurement,'sqlite_writer':sqlite_write,
            'separate_case_saved':separate_case,'original_bytes_executed_once':True,'rollback':rollback}
    (api.root/'local-retry-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    baseline='--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    fault=None
    for point in ('staging','install'):
        flag='--crash-'+point
        if flag in sys.argv:
            assert not baseline and fault is None
            fault=point
            sys.argv.remove(flag)
    for point in ('replace-source','quota-change'):
        if '--'+point in sys.argv:
            assert not baseline and fault is None
            fault=point
            sys.argv.remove('--'+point)
    fixture=FIXTURE
    if baseline:
        fixture="(root/'local-retry-baseline').touch()\n"+fixture
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,fault=fault),fixture_override=fixture))
