"""Measure real CadQuery workflow retry staging and retain real model readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid

import httpx

from live_llm_job_repeat_acceptance import main, put
from live_llm_job_retry_capacity import wait_for


SOURCE='A-geo/capacity-input.bin'
FIXTURE=r'''
from pathlib import Path
import json
import os
import time

def observe(path,identifier):
    root=Path(__file__).parent
    marker=root/'workflow-retry-entered.json'
    if (root/'workflow-retry-arm').is_file() and not marker.exists():
        copies=sorted(item.relative_to(root/'files').as_posix()
            for item in path.rglob('capacity-input.bin') if item.is_file())
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'id':identifier,'copies':copies,'pid':os.getpid()}),encoding='utf8')
        temporary.replace(marker)
        deadline=time.monotonic()+35
        while not (root/'workflow-retry-release').is_file():
            if time.monotonic()>deadline:raise TimeoutError('Actual workflow retry upload did not drain')
            time.sleep(.025)

if Path(__file__).with_name('workflow-retry-baseline').exists():
    from app.services import attempt_workspace
    old=attempt_workspace.Snapshot.prepare
    def prepare(self):
        result=old(self)
        observe(self.target,result['attempt_id'])
        return result
    attempt_workspace.Snapshot.prepare=prepare
else:
    from app import storage
    from app.services import attempt_objects, local_retry_seal
    local=local_retry_seal.prepare_files
    remote=attempt_objects.put_input_members
    def prepare(*args):
        result=local(*args)
        observe(result.directory,result.id)
        return result
    def upload(prefix,base,records,root,*args,**kwargs):
        result=remote(prefix,base,records,root,*args,**kwargs)
        if kwargs.get('archive') is not None and 'A-geo/capacity-input.bin' in records:
            identifier=base.split('/')[-2]
            observe(storage._fs_path(prefix,'.')/base.removesuffix('/inputs'),identifier)
        return result
    local_retry_seal.prepare_files=prepare
    attempt_objects.put_input_members=upload
'''


def scenario(api, *, baseline, fault=False, crash=False, source_read=None, store=None, **_):
    if store:
        from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
        wait_for_s3_response(api,store)

    def until(method, path, condition, *, timeout=180):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=api.call(method,path)
            if condition(value):return value
            time.sleep(.15)
        raise TimeoutError('Actual workflow did not reach expected state at '+path)

    payload=b'actual CadQuery input\n'+b'p'*(12*1024*1024)
    digest=hashlib.sha256(payload).hexdigest()
    base=api.base+'/api/v1'
    headers={'Authorization':'Bearer '+api.token}
    case=api.call('GET',api.prefix)
    other=api.call('POST',f"/workspaces/{case['workspace_id']}/cases",{'name':'Concurrent workflow retry writer'})
    script=("import time, hashlib\nfrom pathlib import Path\nimport cadquery as cq\n"
            "def build_geometry(ctx):\n"
            "    time.sleep(6)\n"
            "    data=Path('A-geo/capacity-input.bin').read_bytes()\n"
            f"    assert hashlib.sha256(data).hexdigest() == {digest!r}\n"
            "    return {'capacity_cube': cq.Workplane('XY').box(1,1,1)}\n")
    with httpx.Client(headers=headers,timeout=150,trust_env=False) as client:
        write=client.post(base+api.prefix+'/upload',params={'path':SOURCE},
                          files={'file':('actual.bin',payload,'application/octet-stream')})
        assert write.status_code==200,write.text[:650]
        preview=put(api,'A-geo/geometry.py',script).get('auto_preview_job')
        if preview:api.wait_job(preview)
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry','expected_revision':revision,
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':100,'max_log_bytes':1048576}},
            extra_headers={'Idempotency-Key':str(uuid.uuid4())})
        route='/workflow-runs/'+flow['id']
        active=until('GET',route,lambda value:any(step['attempts'] for step in value['steps']))
        old=next(attempt for step in active['steps'] for attempt in step['attempts'])
        job_path='/jobs/'+str(old['job_id'])
        until('GET',job_path,lambda value:(value['attempt']['phase']=='running' and value['attempt'].get('process_identity')))
        latest=api.call('GET',route)
        api.call('POST',route+':cancel',{'expected_version':latest['version']})
        previous=until('GET',job_path,lambda value:(value['attempt']['phase']=='cancelled'
            and value['attempt'].get('process_stop',{}).get('verified')))
        assert previous['attempt']['workspace']['files'][SOURCE]['sha256']==digest
        stopped=until('GET',route,lambda value:value['status']=='cancelled')
        state=api.call('GET',api.prefix+'/caseDict')
        if state['workflow_state']!='A':api.advance('A')
        options=api.call('GET',route+'/resume-plan')
        chosen=next(step for step in options['steps'] if step['retry_step']['available'])
        body={name:options[name] for name in ('expected_version','expected_revision')}
        body.update({name:chosen[name] for name in (
            'source_step_id','expected_step_version','source_attempt_id','expected_attempt_version')})
        body.update(mode='retry_step',confirmed=True)
        key={'Idempotency-Key':str(uuid.uuid4())}
        marker=api.root/'workflow-retry-entered.json'
        released=api.root/'workflow-retry-release'
        source_jobs={item['id'] for item in api.call('GET',api.prefix+'/jobs')}
        transient=[]
        def submit():
            deadline=time.monotonic()+20
            while True:
                response=client.post(base+route+':resume',json=body,headers=key)
                if response.status_code!=409:
                    return response
                detail=response.json().get('detail')
                if not isinstance(detail,dict) or detail.get('code') not in {'storage_quota_busy','validation_busy'}:
                    return response
                transient.append(detail['code'])
                if time.monotonic()>=deadline:
                    return response
                time.sleep(.15)
        (api.root/'workflow-retry-arm').touch()
        sqlite_writer=None
        separate_case=False
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(submit)
            twin=None
            try:
                wait_for(marker,pending)
                observation=json.loads(marker.read_text(encoding='utf8'))
                if source_read is None:
                    assert len(observation['copies'])==(0 if store else 2),observation
                else:
                    assert observation['read_bytes']==len(payload),observation
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_writer=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower(),str(error)
                            sqlite_writer=False
                        db.rollback()
                    assert sqlite_writer is not (baseline or source_read=='old')
                if not baseline and source_read!='old':
                    saved=client.put(base+f"/cases/{other['id']}/file",json={'path':'Ref/parallel.txt','content':'ok\n'})
                    assert saved.status_code==200 and not pending.done(),saved.text[:650]
                    separate_case=True
                    if crash:
                        from live_llm_failure_fallback_acceptance import kill_owned_api
                        remote=case['storage_prefix']+'/.vibeflow/attempts/'+observation['id']+'/'
                        written=store.raw_client.head_object(Bucket=store.bucket,Key=remote+'inputs/'+SOURCE)
                        assert written['ContentLength']==len(payload)
                        kill_owned_api(api,observation['pid'])
                    elif fault:
                        relative=SOURCE if source_read is not None else previous['attempt']['workspace']['inputs_path']+'/'+SOURCE
                        archive=api.root/'files'/case['storage_prefix']/relative
                        archive=archive.resolve(strict=True)
                        assert archive.is_relative_to(api.root.resolve())
                        before=archive.stat()
                        archive.write_bytes(b'x'+payload[1:])
                        os.utime(archive,ns=(before.st_atime_ns,before.st_mtime_ns))
                        assert archive.stat().st_mtime_ns==before.st_mtime_ns
                    else:
                        twin=pool.submit(submit)
            finally:
                released.touch()
            if crash:
                from live_llm_outbox_restart_acceptance import restart
                try:
                    pending.result(timeout=30)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The isolated API exited after S3 acknowledgement')
                restart(api)
                assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==source_jobs
                recorded=api.call('POST',route+'/recovery-request',
                    {'key':key['Idempotency-Key'],'action':'resume','payload':body})
                assert recorded['found'] is False
                rejected=observation['id']
                marker.unlink()
                response=submit()
                observation=json.loads(marker.read_text(encoding='utf8'))
                assert observation['id']!=rejected
                assert not store.raw_client.list_objects_v2(Bucket=store.bucket,Prefix=remote).get('Contents')
                history=case['storage_prefix']+'/.vibeflow/attempt-provisions/history/'+rejected+'.json'
                with store.raw_client.get_object(Bucket=store.bucket,Key=history)['Body'] as stream:
                    assert json.load(stream)['outcome']=='rolled_back'
            else:
                response=pending.result(timeout=150)
            if fault:
                try:
                    assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:800]
                    assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==source_jobs
                    assert all(not (api.root/'files'/path).exists() for path in observation.get('copies',[]))
                    check=api.call('POST',route+'/recovery-request',
                        {'key':key['Idempotency-Key'],'action':'resume','payload':body})
                    assert check['found'] is False
                finally:
                    archive.write_bytes(payload)
                    os.utime(archive,ns=(before.st_atime_ns,before.st_mtime_ns))
                rejected=observation['id']
                marker.unlink()
                response=submit()
                observation=json.loads(marker.read_text(encoding='utf8'))
                assert observation['id']!=rejected
            assert response.status_code==202,response.text[:800]
            result=response.json()
            if twin is not None:
                duplicate=twin.result(timeout=150)
                assert duplicate.status_code==202 and duplicate.json()['id']==result['id'],duplicate.text[:800]
        assert result['id']!=flow['id']
        child=next(item for step in result['steps'] for item in step['attempts'])
        done=api.wait_job(child['job_id'])
        attempt=done['attempt']
        assert attempt['retry_of_attempt_id']==old['id'] and attempt['root_job_id']==previous['attempt']['root_job_id']
        assert attempt['workspace']['files']==previous['attempt']['workspace']['files']
        assert attempt['outputs']['publication']['status']=='committed'
        current=until('GET','/workflow-runs/'+result['id'],lambda value:value['status']=='awaiting_confirmation')
        assert current['resumption']['source_run_id']==stopped['id'] and current['resumption']['mode']=='retry_step'
        replay=submit()
        assert replay.status_code==202 and replay.json()['id']==result['id'],replay.text[:800]
        assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==source_jobs|{child['job_id']}
        assert api.call('GET',route)['status']=='cancelled' and api.call('GET',job_path)['status']=='cancelled'
    report={'passed':True,'baseline':baseline,'source_read':source_read,'fixture_bytes':len(payload),'copy_observation':observation,
            'sqlite_writer':sqlite_writer,'separate_case_saved':separate_case,'same_key_replayed':True,
            'concurrent_same_key':not (baseline or fault or crash or source_read=='old'),'native_geometry_publication':True,'old_run_retained':True,
            'changed_archive_rejected_before_commit':fault and source_read is None,
            'changed_live_source_rejected_before_commit':fault and source_read is not None,
            'actual_s3_api_exit_recovered':crash,'same_key_transient_admission_codes':transient,
            'backend':'s3' if store else 'filesystem'}
    (api.root/'workflow-retry-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    api.create_case('Independent workflow retry evidence readback')
    return report


if __name__=='__main__':
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        assert '--s3' not in sys.argv
        sys.argv.remove('--baseline')
    fault='--mutate-archive' in sys.argv
    if fault:
        assert not baseline and '--s3' not in sys.argv
        sys.argv.remove('--mutate-archive')
    crash='--crash-stage' in sys.argv
    if crash:
        assert '--s3' in sys.argv and not fault and not baseline
        sys.argv.remove('--crash-stage')
    fixture="(root/'workflow-retry-baseline').touch()\n"+FIXTURE if baseline else FIXTURE
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,fault=fault,crash=crash),fixture_override=fixture,store_volume_max=8,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
