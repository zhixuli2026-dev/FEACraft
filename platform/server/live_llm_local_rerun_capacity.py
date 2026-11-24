"""Actual filesystem reruns with a second writer during sealed input preparation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
import sqlite3
import sys

import httpx

from live_llm_job_repeat_acceptance import main, put
from live_llm_job_retry_capacity import SOURCE, wait_for


FIXTURE=r'''
from pathlib import Path
import json
import os
import time

def observe(directory,identifier):
    root=Path(__file__).parent
    marker=root/'local-rerun-entered.json'
    if (root/'local-rerun-arm').is_file() and not marker.exists():
        copies=sorted(path.relative_to(root/'files').as_posix() for path in directory.rglob('retry-source.bin'))
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'copies':copies,'id':identifier,'pid':os.getpid()}),encoding='utf8')
        temporary.replace(marker)
        deadline=time.monotonic()+35
        while not (root/'local-rerun-release').is_file():
            if time.monotonic()>deadline:raise TimeoutError('Actual filesystem rerun did not drain')
            time.sleep(.025)

if Path(__file__).with_name('local-rerun-baseline').exists():
    from app.services import attempt_workspace
    original=attempt_workspace.Snapshot.prepare
    def prepare(self):
        result=original(self)
        observe(self.target,result['attempt_id'])
        return result
    attempt_workspace.Snapshot.prepare=prepare
else:
    from app.services import local_rerun_seal
    original=local_rerun_seal.prepare_files
    def prepare(*args):
        result=original(*args)
        if not Path(__file__).with_name('local-rerun-crash').exists():
            observe(result.directory,result.id)
        return result
    local_rerun_seal.prepare_files=prepare
    install=local_rerun_seal.PreparedRerun.install
    def installed(self,*args):
        result=install(self,*args)
        if Path(__file__).with_name('local-rerun-crash').exists():
            observe(result[0],self.id)
        return result
    local_rerun_seal.PreparedRerun.install=installed
'''


def scenario(api, *, baseline, fault, **_):
    first=b'original filesystem input\n'
    current=b'current filesystem input\n'+b'j'*(12*1024*1024)
    replacement=b'x'+current[1:]
    old_sha=hashlib.sha256(first).hexdigest()
    tool_path='Ref/rerun.py'
    proof=api.root/'actual-local-rerun.log'
    put(api,tool_path,('from pathlib import Path\nimport hashlib\n'
        f'with Path({SOURCE!r}).open("rb") as stream: digest=hashlib.file_digest(stream,"sha256").hexdigest()\n'
        'print(digest,flush=True)\n'
        f'with Path({str(proof)!r}).open("a",encoding="utf8") as stream: stream.write(digest+"\\n")\n'))
    put(api,SOURCE,first.decode())
    source=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/rerun.py','input_paths':[tool_path,SOURCE]})
    old=api.wait_job(source['id'])
    assert old_sha in old['log_backlog'] and old['attempt']['process_stop']['verified']
    case=api.call('GET',api.prefix)
    other=api.call('POST',f"/workspaces/{case['workspace_id']}/cases",{'name':'Concurrent filesystem rerun writer'})
    route=f"/jobs/{source['id']}:repeat"
    url=api.base+'/api/v1'
    marker=api.root/'local-rerun-entered.json'
    release=api.root/'local-rerun-release'
    key={'Idempotency-Key':'bounded-local-rerun'}

    def confirm():
        option=api.call('GET',f"/jobs/{source['id']}/operations")
        assert option['rerun']['available']
        return {'action':'rerun',**{name:option[name] for name in (
            'source_attempt_id','source_version','expected_revision','expected_event_sequence')}}

    sqlite_writer=None
    other_saved=False
    same_key_parallel=False
    recovered_install=False
    crash=fault=='crash'
    with httpx.Client(headers={'Authorization':'Bearer '+api.token},timeout=140,trust_env=False) as client:
        stale=confirm()
        response=client.post(url+api.prefix+'/upload',params={'path':SOURCE},
                             files={'file':('current.bin',current,'application/octet-stream')})
        assert response.status_code==200,response.text[:630]
        denied=client.post(url+route,json=stale,headers=key)
        assert denied.status_code==409 and denied.json()['detail']['code']=='input_stale',denied.text[:630]
        confirmed=confirm()
        (api.root/'local-rerun-arm').touch()
        if crash:(api.root/'local-rerun-crash').touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(client.post,url+route,json=confirmed,headers=key)
            duplicate=None
            try:
                wait_for(marker,pending)
                probe=json.loads(marker.read_text(encoding='utf8'))
                assert len(probe['copies'])==2,probe
                under_write=baseline or crash
                assert all(('/.vibeflow/attempts/' in '/'+path)==under_write for path in probe['copies']),probe
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_writer=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            sqlite_writer=False
                        db.rollback()
                    assert sqlite_writer is not under_write,sqlite_writer
                if crash:
                    from live_llm_failure_fallback_acceptance import kill_owned_api
                    kill_owned_api(api,probe['pid'])
                if not under_write:
                    saved=client.put(url+f"/cases/{other['id']}/file",json={'path':'Ref/concurrent.txt','content':'ok\n'})
                    assert saved.status_code==200 and not pending.done(),saved.text[:650]
                    other_saved=True
                    if fault=='api':
                        response=client.put(url+api.prefix+'/file',json={'path':'Ref/concurrent.txt','content':'new event\n'})
                        assert response.status_code==200,response.text[:650]
                        assert confirm()['expected_event_sequence']>confirmed['expected_event_sequence']
                    elif fault=='raw':
                        live=(api.root/'files'/case['storage_prefix']/SOURCE).resolve(strict=True)
                        assert live.is_relative_to(api.root.resolve())
                        before=live.stat()
                        live.write_bytes(replacement)
                        os.utime(live,ns=(before.st_atime_ns,before.st_mtime_ns))
                        after=live.stat()
                        assert (after.st_size,after.st_mtime_ns)==(before.st_size,before.st_mtime_ns)
                        assert confirm()==confirmed
                    else:
                        duplicate=pool.submit(client.post,url+route,json=confirmed,headers=key)
            finally:
                release.touch()
            if crash:
                try:
                    pending.result(timeout=30)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The isolated API exited before committing the rerun')
            else:
                response=pending.result(timeout=140)
            if duplicate is not None:
                replay=duplicate.result(timeout=140)
                assert replay.status_code==response.status_code==202,(response.text[:650],replay.text[:650])
                assert replay.json()['id']==response.json()['id'] and replay.json()['created']!=response.json()['created']
                same_key_parallel=True
        if crash:
            from live_llm_outbox_restart_acceptance import restart
            restart(api)
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}=={source['id']}
            old_attempt=probe['id']
            marker.unlink()
            response=client.post(url+route,json=confirmed,headers=key)
            assert response.status_code==202,response.text[:650]
            probe=json.loads(marker.read_text(encoding='utf8'))
            case_root=api.root/'files'/case['storage_prefix']
            assert not (case_root/'.vibeflow/attempts'/old_attempt).exists() and probe['id']!=old_attempt
            rollback=case_root/'.vibeflow/attempt-provisions/history'/(old_attempt+'.json')
            assert json.loads(rollback.read_text(encoding='utf8'))['outcome']=='rolled_back'
            recovered_install=True
        elif fault:
            assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:650]
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}=={source['id']}
            assert all(not (api.root/'files'/path).exists() for path in probe['copies'])
            prior=probe['id']
            confirmed=confirm()
            marker.unlink()
            response=client.post(url+route,json=confirmed,headers=key)
            probe=json.loads(marker.read_text(encoding='utf8'))
            assert probe['id']!=prior
        assert response.status_code==202,response.text[:650]
        result=response.json()
        done=api.wait_job(result['id'])
        attempt=done['attempt']
        sha=hashlib.sha256(replacement if fault=='raw' else current).hexdigest()
        if not same_key_parallel or result['created']:
            assert attempt['id']==probe['id']
        assert attempt['root_job_id']==result['id'] and attempt['number']==1
        assert set(attempt['workspace']['files'])=={tool_path,SOURCE} and attempt['workspace']['files'][SOURCE]['sha256']==sha
        assert old['attempt']['workspace']['files'][SOURCE]['sha256']==old_sha
        assert proof.read_text(encoding='utf8').splitlines()==[old_sha,sha]
        stable=client.post(url+route,json=confirmed,headers=key)
        assert stable.status_code==202 and stable.json()['id']==result['id'] and not stable.json()['created']
    report={'passed':True,'baseline':baseline,'fault':fault,'fixture_bytes':len(current),'measurement':probe,
            'sqlite_writer':sqlite_writer,'unrelated_case_saved':other_saved,'same_key_parallel':same_key_parallel,
            'old_and_current_executed_once':True,'same_key_replay':True,'installed_before_commit_recovered':recovered_install}
    (api.root/'local-rerun-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    baseline='--baseline' in sys.argv
    if baseline:sys.argv.remove('--baseline')
    fault=None
    for mode in ('api','raw'):
        flag='--edit-'+mode
        if flag in sys.argv:
            assert not baseline and fault is None
            sys.argv.remove(flag)
            fault=mode
    if '--crash-install' in sys.argv:
        assert fault is None and not baseline
        sys.argv.remove('--crash-install')
        fault='crash'
    fixture="(root/'local-rerun-baseline').touch()\n"+FIXTURE if baseline else FIXTURE
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,fault=fault),fixture_override=fixture))
