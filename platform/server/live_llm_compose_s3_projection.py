"""Measure physical S3 caching for real report draft preparation and publication."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import sys
import time
from urllib.parse import urlencode

import httpx


FIXTURE = '''
import json
from contextvars import ContextVar
import time
import uuid
from app.services import compose_input_preparation, compose_handler, report_artifacts
from app.services.private_cache import Cache

capture_stage = ContextVar('compose_fixture_stage',default=None)

def traced(stage, original):
    def call(*args, **kwargs):
        marker = capture_stage.set((stage,uuid.uuid4().hex))
        try:
            return original(*args, **kwargs)
        finally:
            capture_stage.reset(marker)
    return call

compose_input_preparation.capture = traced('preparation',compose_input_preparation.capture)
compose_handler.ComposeHandler.source = traced('freshness',compose_handler.ComposeHandler.source)

actual_fetch = Cache.fetch_many
def fetch(self,items,**kwargs):
    entries = list(items)
    result = actual_fetch(self,entries,**kwargs)
    scan = capture_stage.get()
    phase = root/'report-projection-phase'
    if scan is not None and phase.is_file():
        row = {'stage':scan[0],'scan':scan[1],'phase':phase.read_text(encoding='utf-8'),
               'size':sum(item[1] for item in entries),'paths':[item[0] for item in entries]}
        with (root/'report-projection-cache.jsonl').open('a',encoding='utf-8') as result_file:
            result_file.write(json.dumps(row)+'\\n')
    return result

Cache.fetch_many = fetch

actual_resources = report_artifacts.generated_report_resources
def resources(*args,**kwargs):
    values = actual_resources(*args,**kwargs)
    phase = root/'report-projection-phase'
    scan = capture_stage.get()
    if scan and scan[0]=='preparation' and phase.is_file() and (root/'report-projection-pause').is_file():
        entered = root/'report-projection-entered'
        if not entered.exists():
            entered.touch()
            deadline = time.monotonic()+35
            while not (root/'report-projection-release').exists():
                if time.monotonic()>deadline:
                    raise AssertionError('Report resource pause did not drain')
                time.sleep(.02)
    return values

report_artifacts.generated_report_resources = resources
'''


def submit_during(api, client, url, body, key, edit):
    entered = api.root/'report-projection-entered'
    released = api.root/'report-projection-release'
    armed = api.root/'report-projection-pause'
    entered.unlink(missing_ok=True)
    released.unlink(missing_ok=True)
    armed.touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post,url,json=body,headers={'Idempotency-Key':key})
        try:
            deadline = time.monotonic()+90
            while not entered.exists():
                if pending.done():
                    result = pending.result()
                    raise AssertionError(f'Resource pause was missed: {result.status_code} {result.text[:400]}')
                assert time.monotonic()<deadline
                time.sleep(.025)
            result = client.put(api.base+'/api/v1'+api.prefix+'/file',json=edit,timeout=20)
            assert result.status_code==200,result.text[:400]
        finally:
            released.touch()
            armed.unlink()
        return pending.result(timeout=120)


def scenario(api, *, baseline, store=None, **_):
    from sqlalchemy.engine import make_url
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response
    from live_llm_remote_post_report_acceptance import scenario as solve

    assert store is not None, 'This acceptance requires the real S3 backend'
    wait_for_s3_response(api,store)
    measured = asyncio.run(solve(api,selected=make_url(api.database_url) if api.database_url else None))
    figure = next(item for item in measured['artifacts'] if item['file']=='temperature.png')
    root_sheet = 'styles/root.css'
    nested_sheet = 'styles/nested/colors.css'
    shadow = 'E-report/styles/root.css'
    unrelated = 'Ref/large-unrelated-report-source.bin'
    payload = b'v'*(12*1024*1024+37)
    api.put(root_sheet,'@import "nested/colors.css";\n')
    api.put(nested_sheet,f'h1 {{ color: #135c72; background-image: url("../../{figure["path"]}"); }}\n')
    detail = api.call('PUT',api.prefix+'/caseDict/block',{'block':'report',
        'entries':{'template':'thermal','css':'styles/root.css'}})
    output = 'E-report/report.generated.md'
    body = {'output':output,'overwrite':True,'expected_revision':detail['dict_revision']}
    target = api.base+'/api/v1'+api.prefix
    route = target+'/report:compose?wait=false'
    key = 'report-s3-selection-stable'
    phase = api.root/'report-projection-phase'
    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(headers=headers,timeout=120,trust_env=False) as client:
        written = client.post(target+'/upload',params={'path':unrelated},
            files={'file':('large.bin',payload,'application/octet-stream')})
        assert written.status_code==200,written.text[:400]
        phase.write_text('stable',encoding='utf-8')
        if baseline:
            response = client.post(route,json=body,headers={'Idempotency-Key':key})
        else:
            response = submit_during(api,client,route,body,key,
                {'path':'Ref/changed-during-selection.txt','content':'independent file\n'})
        assert response.status_code==200,response.text[:500]
        receipt = response.json()
        job = api.wait_job(receipt['job_id'],timeout=240)
        result = api.call('GET',api.prefix+'/report/compose-result?'+urlencode({'publication_id':receipt['publication_id']}))
        assert result['publication']['applied']
        assert result['chapter_schema'][5]['figures'][0]['path']==figure['path']
        frozen = job['attempt']['workspace']['files']
        assert set((root_sheet,nested_sheet,figure['path']))<=frozen.keys() and unrelated not in frozen

        traces = [json.loads(line) for line in (api.root/'report-projection-cache.jsonl').read_text(encoding='utf-8').splitlines()]
        initial = [row for row in traces if row['phase']=='stable' and row['stage']=='preparation']
        freshness = [row for row in traces if row['phase']=='stable' and row['stage']=='freshness']
        assert initial and freshness
        cached = {path for row in initial for path in row['paths']}
        checked = {path for row in freshness for path in row['paths']}
        assert (unrelated in cached)==baseline and (unrelated in checked)==baseline
        assert {root_sheet,nested_sheet,figure['path']}<=cached
        assert {root_sheet,nested_sheet,figure['path']}<=checked
        if not baseline:
            case_jobs = {j['id'] for j in api.call('GET',api.prefix+'/jobs')}
            phase.write_text('shadow',encoding='utf-8')
            shadow_css = 'h1 { color: #334455; }\n'
            stale = submit_during(api,client,route,body,'report-s3-selection-shadow',
                {'path':shadow,'content':shadow_css})
            assert stale.status_code==409 and stale.json()['detail']['code']=='input_stale',stale.text[:400]
            assert {j['id'] for j in api.call('GET',api.prefix+'/jobs')}==case_jobs
            phase.write_text('retry',encoding='utf-8')
            retry = client.post(route,json=body,headers={'Idempotency-Key':'report-s3-selection-shadow'})
            assert retry.status_code==200,retry.text[:400]
            final = api.wait_job(retry.json()['job_id'],timeout=240)
            files = final['attempt']['workspace']['files']
            assert files[shadow]['sha256']==hashlib.sha256(shadow_css.encode()).hexdigest()
            assert root_sheet not in files and nested_sheet not in files
        persisted = api.call('GET',api.prefix+'/raw?'+urlencode({'path':unrelated}),raw=True)
        assert len(persisted)==len(payload) and hashlib.sha256(persisted).digest()==hashlib.sha256(payload).digest()
    report = {'passed':True,'baseline':baseline,'unrelated_bytes':len(payload),'source_selection':sorted(cached),
              'preparation_body_bytes':sum(row['size'] for row in initial),
              'first_freshness_body_bytes':sum(row['size'] for row in freshness if row['scan']==freshness[0]['scan']),
              'actual_temperature_k':measured['mean_temperature_k'],'unrelated_write_accepted':not baseline,
              'new_higher_priority_resource_rejected':not baseline}
    (api.root/'compose-s3-projection.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__=='__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER
    baseline = '--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline),fixture_override=FIXTURE,
        store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
