"""Exercise real draft submission while holding a completed template resource read."""
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


FIXTURE = '''
import time
from contextlib import asynccontextmanager
from app.services import case_read_capacity
from app.services import report_artifacts
read_actual_resources = report_artifacts.generated_report_resources
original_deadline = case_read_capacity.file_read_deadline

@asynccontextmanager
async def test_deadline(code, label, *, seconds=None):
    if code=='compose_preparation' and (root/'compose-resources-expire').exists():
        seconds = 1.5
    async with original_deadline(code, label, seconds=seconds):
        yield

case_read_capacity.file_read_deadline = test_deadline

def observed_report_resources(workspace, options, figures, original, selections, **kwargs):
    values = read_actual_resources(workspace, options, figures, original, selections, **kwargs)
    if (root/'compose-resources-arm').exists() and 'E-report/styles/load.css' in values:
        entered = root/'compose-resources-entered'
        if not entered.exists():
            entered.write_text(str(len(values['E-report/styles/load.css'])), encoding='utf-8')
            stop = time.monotonic()+30
            while not (root/'compose-resources-release').exists():
                if time.monotonic()>=stop:
                    raise AssertionError('Test failed to release the actual resource read')
                time.sleep(.025)
    return values

report_artifacts.generated_report_resources = observed_report_resources
'''


def scenario(api, *, baseline, expire, store=None, **_):
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
    from live_llm_remote_post_report_acceptance import scenario as solve
    from sqlalchemy.engine import make_url

    wait_for_s3_response(api,store)
    result = asyncio.run(solve(api,selected=make_url(api.database_url) if api.database_url else None))
    workspace = api.call('GET',api.prefix)['workspace_id']
    other = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Independent report preparation writer'})
    base = api.base+'/api/v1'
    resource = 'E-report/styles/load.css'
    original = b'/*'+b'x'*(4*1024*1024)+b'*/\nh1 {color: #135c72;}\n'
    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(headers=headers,timeout=120,trust_env=False) as client:
        uploaded = client.post(base+api.prefix+'/upload', params={'path':resource},
            files={'file':('load.css',original,'text/css')})
        assert uploaded.status_code==200, uploaded.text[:400]
        modified = api.call('PUT',api.prefix+'/caseDict/block',{'block':'report',
            'entries':{'template':'thermal','css':'styles/load.css'}})
        output = 'E-report/report.generated.md'
        key = 'report-source-lock-capacity'
        body = {'output':output,'overwrite':True,'expected_revision':modified['dict_revision']}
        route = base+api.prefix+'/report:compose?wait=false'
        (api.root/'compose-resources-arm').touch()
        entered = api.root/'compose-resources-entered'
        release = api.root/'compose-resources-release'
        independent = False
        sqlite_writer = None
        before = {job['id'] for job in api.call('GET',api.prefix+'/jobs')}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post,route,json=body,headers={'Idempotency-Key':key})
            try:
                until = time.monotonic()+80
                while not entered.exists():
                    if pending.done():
                        response = pending.result()
                        raise AssertionError(f'Did not reach resource read: {response.status_code} {response.text[:500]}')
                    assert time.monotonic()<until, 'Actual stylesheet read did not reach its pause'
                    time.sleep(.025)
                assert int(entered.read_text(encoding='utf-8'))==len(original)
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            sqlite_writer = True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            sqlite_writer = False
                        db.rollback()
                    assert sqlite_writer is not baseline
                if not baseline:
                    saved = client.put(base+f"/cases/{other['id']}/file",
                        json={'path':'Ref/during-prepare.txt','content':'other case can write\n'},timeout=12)
                    assert saved.status_code==200,saved.text[:400]
                    independent = True
                    replacement = 'h1 {color: #8e311f;}\n'
                    changed = client.put(base+api.prefix+'/file',
                        json={'path':resource,'content':replacement},timeout=12)
                    assert changed.status_code==200,changed.text[:400]
            finally:
                release.touch()
            response = pending.result(timeout=120)
        if baseline:
            assert response.status_code==200,response.text[:400]
            receipt = response.json()
        else:
            assert response.status_code==409 and response.json()['detail']['code']=='input_stale', response.text[:400]
            assert {job['id'] for job in api.call('GET',api.prefix+'/jobs')}==before
            retry = client.post(route,json=body,headers={'Idempotency-Key':key})
            assert retry.status_code==200,retry.text[:400]
            receipt = retry.json()
        job = api.wait_job(receipt['job_id'],timeout=240)
        publication = api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'publication_id':receipt['publication_id']}))
        assert publication['publication']['applied'] and publication['template']['id']=='thermal'
        expected = original if baseline else replacement.encode()
        assert job['attempt']['workspace']['files'][resource]['sha256']==hashlib.sha256(expected).hexdigest()
        replay = client.post(route,json=body,headers={'Idempotency-Key':key})
        assert replay.status_code==200 and replay.json()['job_id']==receipt['job_id']
        if not baseline:
            verify_preconditions(api,client,route,body)
        timeout_recovered = verify_deadline(api,client,route,body) if expire else None
    proof = {'passed':True,'baseline':baseline,'actual_temperature_k':result['mean_temperature_k'],
             'actual_css_bytes_read':len(original),'independent_sqlite_writer':sqlite_writer,
             'independent_case_write':independent,'stale_rejected_and_retried':not baseline,
             'job_id':receipt['job_id'],'one_idempotent_submission':True,'timeout_recovery':timeout_recovered,
             'preconditions_verified':not baseline}
    (api.root/'compose-preparation-capacity.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    return proof


def verify_preconditions(api,client,route,body):
    before = {job['id'] for job in api.call('GET',api.prefix+'/jobs')}
    for update, code in [
        ({'expected_sha256':'0'*64},'file_conflict'),
        ({'overwrite':False},'file_conflict'),
        ({'expected_revision':body['expected_revision']+1},'revision_conflict'),
    ]:
        response = client.post(route,json={**body,**update})
        assert response.status_code==409 and response.json()['detail']['code']==code, response.text[:400]
    assert {job['id'] for job in api.call('GET',api.prefix+'/jobs')}==before


def verify_deadline(api,client,route,body):
    before = {job['id'] for job in api.call('GET',api.prefix+'/jobs')}
    armed = api.root/'compose-resources-expire'
    entered = api.root/'compose-resources-entered'
    release = api.root/'compose-resources-release'
    entered.unlink()
    release.unlink()
    key = 'report-source-lock-deadline'
    armed.touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post,route,json=body,headers={'Idempotency-Key':key})
        try:
            until = time.monotonic()+20
            while not entered.exists():
                assert not pending.done(), 'Deadline request did not reach the actual body read'
                assert time.monotonic()<until, 'Waiting for the deadline case fixture'
                time.sleep(.025)
            time.sleep(1.8)
            assert not pending.done(), 'The aborted worker released its request before the actual read drained'
        finally:
            release.touch()
        expired = pending.result(timeout=30)
    armed.unlink()
    assert expired.status_code==504 and expired.json()['detail']['code']=='compose_preparation_timeout', expired.text[:400]
    assert {job['id'] for job in api.call('GET',api.prefix+'/jobs')}==before
    retried = client.post(route,json=body,headers={'Idempotency-Key':key})
    assert retried.status_code==200, retried.text[:400]
    api.wait_job(retried.json()['job_id'],timeout=240)
    return {'status':504,'worker_drained_before_reply':True,'no_job_at_timeout':True,
            'retry_job':retried.json()['job_id']}


if __name__=='__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline = '--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    expire = '--expire' in sys.argv
    if expire:
        sys.argv.remove('--expire')
    assert not (baseline and expire)
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,expire=expire),fixture_override=FIXTURE,
        store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
