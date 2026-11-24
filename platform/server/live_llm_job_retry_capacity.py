"""Observe a real failed job retry on S3 after the immutable upload has drained."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import sqlite3
import sys
import time

import httpx


SOURCE = 'Ref/retry-source.bin'
FIXTURE = '''
import json
import os
from pathlib import Path
import time
from app.services import attempt_objects

actual = attempt_objects.put_input_members
def observe(prefix,base,records,*args,**kwargs):
    result=actual(prefix,base,records,*args,**kwargs)
    name='Ref/retry-source.bin'
    marker=root/'retry-capacity-entered.json'
    if name in records and (root/'retry-capacity-arm').is_file() and not marker.exists():
        attempt=base.split('/')[-2]
        target=root/'files'/prefix/'.vibeflow/attempts'/attempt
        selected=(root/'files'/'.attempt-sources',target)
        paths=[]
        for directory in selected:
            paths.extend(path.relative_to(root/'files').as_posix() for path in directory.rglob('retry-source.bin')
                         if path.is_file() and path.stat().st_size==records[name]['size'])
        temporary=marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({'id':attempt,'bytes':records[name]['size'],'copies':sorted(paths),
                                         'pid':os.getpid()}),encoding='utf8')
        temporary.replace(marker)
        deadline=time.monotonic()+35
        while not (root/'retry-capacity-release').is_file():
            if time.monotonic()>deadline:
                raise TimeoutError('Actual retry input upload did not drain')
            time.sleep(.025)
    return result

attempt_objects.put_input_members=observe
'''
FALLBACK_FIXTURE = '''
import asyncio
from contextvars import ContextVar
import json
import time
from app.models import DurableEvent
from app.services import workflow_fallback

route = ContextVar('retry_fallback_request',default=False)
actual_settle = workflow_fallback.settle_failure_fallback
actual_retry = workflow_fallback.settle_retry_source

async def observe_settle(db,case,job):
    requested=root/'retry-fallback-source-id'
    selected=requested.is_file() and int(requested.read_text('ascii'))==job.id
    if selected and not route.get():
        (root/'retry-fallback-background-paused').touch()
        deadline=time.monotonic()+85
        while not (root/'retry-fallback-background-release').is_file():
            if time.monotonic()>deadline:
                raise TimeoutError('Deferred original fallback did not drain')
            await asyncio.sleep(.03)
    result=await actual_settle(db,case,job)
    if selected and route.get():
        receipt=await db.get(DurableEvent,workflow_fallback.event_id(job,workflow_fallback.APPLIED))
        assert receipt is not None
        (root/'retry-fallback-original-applied.json').write_text(
            json.dumps({'id':receipt.id,'case_id':receipt.case_id,'attempt_id':receipt.attempt_id}),encoding='utf8')
    return result

async def observe_retry(db,case,job):
    token=route.set(True)
    try:
        return await actual_retry(db,case,job)
    finally:
        route.reset(token)

workflow_fallback.settle_failure_fallback=observe_settle
workflow_fallback.settle_retry_source=observe_retry
'''


def wait_for(marker,pending):
    deadline=time.monotonic()+120
    while not marker.exists():
        if pending.done():
            response=pending.result()
            raise AssertionError(f'Retry pause missed: {response.status_code} {response.text[:600]}')
        assert time.monotonic()<deadline
        time.sleep(.025)


def scenario(api, *, baseline, fault, store=None, **_):
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
    from live_llm_job_repeat_acceptance import put, write

    assert store is not None
    wait_for_s3_response(api,store)
    original=b'original sealed byte\n'+b'a'*(12*1024*1024)
    original_sha=hashlib.sha256(original).hexdigest()
    current_sha=hashlib.sha256(b'different live bytes\n').hexdigest()
    gate=api.root/'retry-capacity-condition-resolved'
    proof_file=api.root/'actual-retry-proof.log'
    code=("from pathlib import Path\nimport hashlib\n"
          "with Path('Ref/retry-source.bin').open('rb') as stream:\n"
          "    digest=hashlib.file_digest(stream,'sha256').hexdigest()\nprint(digest,flush=True)\n"
          f"if not Path({str(gate)!r}).is_file(): raise SystemExit(19)\n"
          f"with Path({str(proof_file)!r}).open('a',encoding='utf8') as stream: stream.write(digest+'\\n')\n")
    put(api,'Ref/retry-capacity.py',code)
    case=api.call('GET',api.prefix)
    other=api.call('POST',f"/workspaces/{case['workspace_id']}/cases",{'name':'Concurrent retry writer'})
    base=api.base+'/api/v1'
    headers={'Authorization':'Bearer '+api.token}
    marker=api.root/'retry-capacity-entered.json'
    release=api.root/'retry-capacity-release'
    key='actual-bounded-retry'
    sqlite_writer=None
    other_case=False
    recovered=None
    with httpx.Client(headers=headers,timeout=160,trust_env=False) as client:
        uploaded=client.post(base+api.prefix+'/upload',params={'path':SOURCE},
                             files={'file':('source.bin',original,'application/octet-stream')})
        assert uploaded.status_code==200,uploaded.text[:400]
        first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/retry-capacity.py',
                       'input_paths':['Ref/retry-capacity.py',SOURCE]})
        failed=api.wait_job(first['id'],expected='failed')
        assert failed['exit_code']==19 and original_sha in failed['log_backlog']
        assert failed['attempt']['process_stop']['verified']
        option=api.call('GET',f"/jobs/{first['id']}/operations")
        assert option['retry']['available']
        put(api,SOURCE,'different live bytes\n')
        archived_revision=failed['attempt']['workspace']['source']['dict_revision']
        live_revision=archived_revision
        if not baseline:
            write(api,'PUT',api.prefix+'/caseDict/block',{'block':'report','entries':{'css':'styles/retry-only.css'}})
            live_revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            assert live_revision>archived_revision
        gate.touch()
        route=base+f"/jobs/{first['id']}:repeat"
        body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
        jobs_before={row['id'] for row in api.call('GET',api.prefix+'/jobs')}
        race=None
        (api.root/'retry-capacity-arm').touch()
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending=workers.submit(client.post,route,json=body,headers={'Idempotency-Key':key})
            try:
                wait_for(marker,pending)
                probe=json.loads(marker.read_text(encoding='utf8'))
                assert probe['bytes']==len(original) and len(probe['copies'])==(3 if baseline else 0),probe
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_writer=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            sqlite_writer=False
                        db.rollback()
                    assert sqlite_writer is not baseline,sqlite_writer
                if not baseline:
                    saved=client.put(base+f"/cases/{other['id']}/file",json={'path':'Ref/parallel.txt','content':'written\n'},timeout=15)
                    assert saved.status_code==200 and not pending.done(),saved.text[:400]
                    other_case=True
                if fault:
                    pending_path=case['storage_prefix']+'/.vibeflow/attempt-provisions/pending/'+probe['id']+'.json'
                    raw=store.raw_client
                    with raw.get_object(Bucket=store.bucket,Key=pending_path)['Body'] as stream:
                        draft=json.load(stream)
                    assert draft['job_id'] is None and draft['manifest']['source']['retry_of_attempt_id']==option['source_attempt_id']
                    if fault=='crash':
                        from live_llm_failure_fallback_acceptance import kill_owned_api
                        kill_owned_api(api,probe['pid'])
                    else:
                        old_key=case['storage_prefix']+'/'+failed['attempt']['workspace']['inputs_path']+'/'+SOURCE
                        raw.put_object(Bucket=store.bucket,Key=old_key,Body=b'x'+original[1:])
                elif not baseline:
                    race=workers.submit(client.post,route,json=body,headers={'Idempotency-Key':key})
                    time.sleep(.2)
            finally:
                release.touch()
            if fault=='crash':
                try:
                    pending.result(timeout=25)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The original API exited before the retry response')
            else:
                response=pending.result(timeout=160)
        if fault:
            if fault=='crash':
                from live_llm_outbox_restart_acceptance import restart
                restart(api)
            else:
                try:
                    assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:700]
                finally:
                    raw.put_object(Bucket=store.bucket,Key=old_key,Body=original)
            assert {row['id'] for row in api.call('GET',api.prefix+'/jobs')}==jobs_before
            response=client.post(route,json=body,headers={'Idempotency-Key':key})
            history=case['storage_prefix']+'/.vibeflow/attempt-provisions/history/'+probe['id']+'.json'
            with raw.get_object(Bucket=store.bucket,Key=history)['Body'] as stream:
                outcome=json.load(stream)
            assert outcome['outcome']=='rolled_back' and outcome['plan']==draft
            orphan=case['storage_prefix']+'/.vibeflow/attempts/'+probe['id']+'/'
            assert not raw.list_objects_v2(Bucket=store.bucket,Prefix=orphan).get('Contents')
            recovered={'fault':fault,'old_retry_job_absent':True,'unbound_objects_removed':True,'same_key_retried':True}
        assert response.status_code==202,response.text[:750]
        result=response.json()
        done=api.wait_job(result['id'],timeout=240)
        assert (done['attempt']['id']!=probe['id'])==bool(fault)
        assert done['attempt']['retry_of_attempt_id']==option['source_attempt_id']
        assert done['attempt']['workspace']['files']==failed['attempt']['workspace']['files']
        assert done['attempt']['workspace']['source']['dict_revision']==archived_revision
        assert original_sha in done['log_backlog'] and current_sha not in done['log_backlog']
        replay=client.post(route,json=body,headers={'Idempotency-Key':key})
        assert replay.status_code==202 and replay.json()['id']==result['id'] and not replay.json()['created']
        if race is not None:
            concurrent=race.result()
            assert concurrent.status_code==202 and concurrent.json()['id']==result['id'],concurrent.text[:600]
            assert int(concurrent.json()['created'])+int(result['created'])==1
        second=client.post(route,json=body,headers={'Idempotency-Key':key+'-another'})
        assert second.status_code==409 and second.json()['detail']['code']=='state_conflict'
        assert proof_file.read_text(encoding='utf8').splitlines()==[original_sha]
        assert api.call('GET',f"/jobs/{first['id']}")['status']=='failed'
    result={'passed':True,'baseline':baseline,'after_upload_before_commit':probe,'sqlite_writer':sqlite_writer,
            'unrelated_case_saved':other_case,'original_sha256':original_sha,'live_sha256':current_sha,
            'original_attempt_preserved':True,'original_bytes_executed_once':True,'same_key_replayed':True,
            'archived_revision':archived_revision,'current_revision':live_revision,
            'simultaneous_same_key':race is not None,'recovery':recovered}
    (api.root/'job-retry-capacity.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


def fallback_race(api,*,store=None,**_):
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
    from live_llm_job_repeat_acceptance import put,repeat

    wait_for_s3_response(api,store)
    start=api.root/'retry-geometry-allow-first-exit'
    repaired=api.root/'retry-geometry-can-build'
    paused=api.root/'retry-fallback-background-paused'
    release=api.root/'retry-fallback-background-release'
    script=("from pathlib import Path\nimport time\nimport cadquery as cq\n"
            f"deadline=time.monotonic()+28\nwhile not Path({str(start)!r}).is_file():\n"
            "    if time.monotonic()>deadline: raise TimeoutError('test driver did not allow original failure')\n"
            "    time.sleep(.025)\n"
            f"if not Path({str(repaired)!r}).is_file(): raise RuntimeError('transient native condition')\n"
            "def build_geometry(ctx): return {'box':cq.Workplane('XY').box(1,2,3)}\n")
    put(api,'A-geo/geometry.py',script)
    first=api.call('POST',api.prefix+'/geometry:preview')['job_id']
    (api.root/'retry-fallback-source-id').write_text(str(first),encoding='ascii')
    start.touch()
    try:
        old=api.wait_job(first,expected='failed')
        assert old['attempt']['process_stop']['verified']
        deadline=time.monotonic()+30
        while not paused.exists():
            assert time.monotonic()<deadline
            time.sleep(.025)
        repaired.touch()
        option=api.call('GET',f'/jobs/{first}/operations')
        assert option['retry']['available']
        body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
        key='cadquery-original-fallback-before-retry'
        child=repeat(api,first,body,key)
        applied=json.loads((api.root/'retry-fallback-original-applied.json').read_text('utf8'))
        assert applied['attempt_id']==old['attempt']['id'] and not release.exists()
        run=api.wait_job(child['id'],timeout=240)
        assert run['attempt']['handler']['key']=='geometry.preview'
        assert run['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(script.encode()).hexdigest()
        assert run['attempt']['outputs']['publication']['status']=='committed'
        replay=repeat(api,first,body,key)
        assert replay['id']==child['id'] and not replay['created']
    finally:
        release.touch()
    report={'passed':True,'old_job':first,'retry_job':child['id'],'background_fallback_delayed':True,
            'original_fallback_committed_by_retry_request':applied,'cadquery_publication_committed':True,
            'source_stop_verified':True,'same_key_replay':True}
    (api.root/'job-retry-fallback-race.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    fault=None
    for flag in ('crash','replace-source'):
        if '--'+flag in sys.argv:
            assert not baseline and fault is None
            sys.argv.remove('--'+flag)
            fault=flag
    selected=partial(scenario,baseline=baseline,fault=fault)
    fixture=FIXTURE
    if '--fallback-race' in sys.argv:
        assert not baseline and fault is None
        sys.argv.remove('--fallback-race')
        selected,fixture=fallback_race,FALLBACK_FIXTURE
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=selected,fixture_override=fixture,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
