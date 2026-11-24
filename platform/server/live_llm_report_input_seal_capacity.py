"""Measure S3 report input copies and SQL concurrency with an actual draft backup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import sqlite3
import sys
import time
from urllib.parse import urlencode

import httpx


OLD = 'E-report/report.generated.md'
FIXTURE = '''
import json,os,time
from pathlib import Path
from app.services import attempt_objects
original_put = attempt_objects.put_input_members

def measured_put(prefix,base,records,source,*args):
    result = original_put(prefix,base,records,source,*args)
    old = 'E-report/report.generated.md'
    marker = root/'report-input-seal-entered.json'
    if (root/'report-input-seal-arm').is_file() and records.get(old,{}).get('size',0)>8*1024*1024 and not marker.exists():
        report = {'old_draft_bytes':records[old]['size'],'attempt':base.split('/')[-2],
                  'staged_input':str(source).replace('\\\\','/').startswith(str(root/'files').replace('\\\\','/').rstrip('/')+'/.attempt-sources/'),
                  'local_draft_copies':[]}
        for directory in (root/'files'/'.attempt-sources', Path(source).parent):
            if directory.is_dir():
                for candidate in directory.rglob('report.generated.md'):
                    if candidate.is_file() and candidate.stat().st_size==records[old]['size']:
                        relative = candidate.relative_to(root/'files').as_posix()
                        if relative not in report['local_draft_copies']:
                            report['local_draft_copies'].append(relative)
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(report),encoding='utf-8')
        temporary.replace(marker)
        until = time.monotonic()+35
        while not (root/'report-input-seal-release').is_file():
            if time.monotonic()>until:
                raise TimeoutError('The test did not release the actual S3 seal')
            time.sleep(.025)
    return result

attempt_objects.put_input_members = measured_put
(root/'report-input-seal-api-pid').write_text(str(os.getpid()),encoding='ascii')
'''


def scenario(api, *, baseline, crash, store=None, **_):
    from sqlalchemy.engine import make_url
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
    from live_llm_remote_post_report_acceptance import scenario as solve

    assert store is not None
    wait_for_s3_response(api,store)
    measured = asyncio.run(solve(api,selected=make_url(api.database_url) if api.database_url else None))
    case = api.call('GET',api.prefix)
    workspace = case['workspace_id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent draft-seal writer'})
    old = b'Previous draft before the revised report.\n'+b'd'*(12*1024*1024)
    sha = hashlib.sha256(old).hexdigest()
    headers = {'Authorization':'Bearer '+api.token}
    key = 'actual-report-stage-seal'
    body = {'output':OLD,'overwrite':True,'expected_sha256':sha,
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision']}
    base = api.base+'/api/v1'
    route = base+api.prefix+'/report:compose?wait=false'
    marker = api.root/'report-input-seal-entered.json'
    release = api.root/'report-input-seal-release'
    independent_case = False
    sqlite_write = None
    recovered = None
    with httpx.Client(headers=headers,timeout=120,trust_env=False) as client:
        uploaded = client.post(base+api.prefix+'/upload',params={'path':OLD},
                               files={'file':('old.md',old,'text/markdown')})
        assert uploaded.status_code==200,uploaded.text[:500]
        (api.root/'report-input-seal-arm').touch()
        prior_jobs = {item['id'] for item in api.call('GET',api.prefix+'/jobs')}
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=worker.submit(client.post,route,json=body,headers={'Idempotency-Key':key})
            try:
                until=time.monotonic()+105
                while not marker.exists():
                    if pending.done():
                        response=pending.result()
                        raise AssertionError(f'Input PUT pause missed: {response.status_code} {response.text[:600]}')
                    assert time.monotonic()<until
                    time.sleep(.025)
                probe=json.loads(marker.read_text(encoding='utf-8'))
                assert probe['old_draft_bytes']==len(old)
                assert probe['staged_input'] is not baseline
                assert len(probe['local_draft_copies'])==(3 if baseline else 1),probe
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_write=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            sqlite_write=False
                        db.rollback()
                    assert sqlite_write is not baseline, sqlite_write
                if not baseline:
                    saved=client.put(base+f"/cases/{other['id']}/file",
                                     json={'path':'Ref/wrote-during-seal.txt','content':'ok\n'},timeout=15)
                    assert saved.status_code==200,saved.text[:400]
                    independent_case=True
                    assert not pending.done()
                if crash:
                    from live_llm_failure_fallback_acceptance import kill_owned_api
                    pid=int((api.root/'report-input-seal-api-pid').read_text(encoding='ascii'))
                    name=case['storage_prefix']+'/.vibeflow/attempt-provisions/pending/'+probe['attempt']+'.json'
                    plan=store.raw_client.get_object(Bucket=store.bucket,Key=name)
                    with plan['Body'] as stream:
                        draft=json.load(stream)
                    assert draft['schema']=='vibeflow.attempt-seal-draft.v1' and draft['job_id'] is None
                    kill_owned_api(api,pid)
            finally:
                release.touch()
            if crash:
                try:
                    pending.result(timeout=30)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The API exited before returning a submission')
            else:
                response=pending.result(timeout=120)
        if crash:
            from live_llm_outbox_restart_acceptance import restart
            restart(api)
            assert int((api.root/'report-input-seal-api-pid').read_text(encoding='ascii'))!=pid
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}==prior_jobs
            response=client.post(route,json=body,headers={'Idempotency-Key':key})
            name=case['storage_prefix']+'/.vibeflow/attempt-provisions/history/'+probe['attempt']+'.json'
            recovered_plan=store.raw_client.get_object(Bucket=store.bucket,Key=name)
            with recovered_plan['Body'] as stream:
                resolution=json.load(stream)
            assert resolution['outcome']=='rolled_back' and resolution['plan']==draft
            orphan=case['storage_prefix']+'/.vibeflow/attempts/'+probe['attempt']+'/'
            assert not store.raw_client.list_objects_v2(Bucket=store.bucket,Prefix=orphan).get('Contents')
            recovered={'exited_api_pid':pid,'rolled_back_attempt':probe['attempt'],'old_sql_job_absent':True,
                       'old_objects_absent':True,'same_key_retried':True}
        assert response.status_code==200,response.text[:800]
        result=response.json()
        job=api.wait_job(result['job_id'],timeout=240)
        sealed=job['attempt']['workspace']
        assert (sealed['attempt_id']!=probe['attempt'])==crash
        assert sealed['files'][OLD]['sha256']==sha
        assert sealed['publications'] and sealed['publications'][0]['kind']=='postprocess'
        publication=api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'publication_id':result['publication_id']}))
        assert publication['publication']['applied']
        uri=base+api.prefix+'/report/compose-artifact?'+urlencode(
            {'publication_id':result['publication_id'],'path':publication['backup'],'output':OLD})
        downloaded=client.get(uri)
        assert downloaded.status_code==200 and len(downloaded.content)==len(old)
        assert hashlib.sha256(downloaded.content).hexdigest()==sha
        replay=client.post(route,json=body,headers={'Idempotency-Key':key})
        assert replay.status_code==200 and replay.json()['job_id']==result['job_id']
    report={'passed':True,'baseline':baseline,'actual_temperature_k':measured['mean_temperature_k'],
            'seal':probe,'independent_sqlite_writer':sqlite_write,'independent_case_writer':independent_case,
            'archive_sha256_verified':True,'upstream_publication_bound':True,'replay_job':result['job_id'],
            'crash_recovery':recovered}
    (api.root/'report-input-seal-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    crash='--crash' in sys.argv
    if crash:
        assert not baseline
        sys.argv.remove('--crash')
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,crash=crash),fixture_override=FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=12,authenticated=True)))
