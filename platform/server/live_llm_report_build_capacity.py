"""Measure real case downloads and SQL concurrency before Pandoc report compilation."""
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


UNRELATED = 'Ref/untouched-during-report.bin'
FIXTURE = '''
import asyncio
from contextvars import ContextVar
import json
import time
import uuid
from app.services import report_handler
from app.services.private_cache import Cache

phase = ContextVar('measure_formal_report',default=None)

def observe(stage,method):
    async def call(*args,**kwargs):
        token=phase.set((stage,uuid.uuid4().hex))
        try:
            result=await method(*args,**kwargs)
            entered=root/'formal-report-pandoc-entered'
            if stage=='pandoc' and (root/'measure-formal-report-conflict').is_file() and not entered.exists():
                entered.touch()
                limit=time.monotonic()+30
                while not (root/'formal-report-pandoc-release').is_file():
                    if time.monotonic()>limit:
                        raise TimeoutError('The test did not release verified report preparation')
                    await asyncio.sleep(.025)
            return result
        finally:
            phase.reset(token)
    return call

handler=report_handler.ReportHandler
handler.prepare=observe('seed',handler.prepare)
handler.complete_preparation=observe('pandoc',handler.complete_preparation)
fetch=Cache.fetch_many

def record(self,items,**kwargs):
    rows=list(items)
    result=fetch(self,rows,**kwargs)
    stage=phase.get()
    if stage is not None and (root/'measure-formal-report').is_file() and '.case-read-views' in self.root.parts:
        row={'stage':stage[0],'scan':stage[1],'bytes':sum(item[1] for item in rows),'paths':[item[0] for item in rows]}
        with (root/'formal-report-caches.jsonl').open('a',encoding='utf8') as log:
            log.write(json.dumps(row)+'\\n')
        entered=root/'formal-report-seed-entered'
        if stage[0]=='seed' and 'E-report/report.md' in row['paths'] and not entered.exists():
            entered.touch()
            limit=time.monotonic()+30
            while not (root/'formal-report-seed-release').is_file():
                if time.monotonic()>limit:
                    raise TimeoutError('The test did not release report seed reading')
                time.sleep(.025)
    return result

Cache.fetch_many=record
'''


def wait_for_marker(marker, pending):
    limit=time.monotonic()+115
    while not marker.exists():
        if pending.done():
            returned=pending.result()
            raise AssertionError(f'{marker.name} missed: {returned.status_code} {returned.text[:550]}')
        assert time.monotonic()<limit
        time.sleep(.025)


def scenario(api, *, baseline, conflict, store=None, **_):
    from sqlalchemy.engine import make_url
    from live_llm_engineering_report_schema_acceptance import download, verify_embedded, wait_for_s3_response
    from live_llm_remote_post_report_acceptance import scenario as solve

    assert store is not None
    wait_for_s3_response(api,store)
    measured=asyncio.run(solve(api,selected=make_url(api.database_url) if api.database_url else None))
    payload=b'unused case file\n'+b'f'*(12*1024*1024)
    workspace=api.call('GET',api.prefix)['workspace_id']
    other=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Concurrent report seed writer'})
    base=api.base+'/api/v1'
    route=base+api.prefix+'/report:build'
    entered=api.root/'formal-report-seed-entered'
    release=api.root/'formal-report-seed-release'
    native_entered=api.root/'formal-report-pandoc-entered'
    native_release=api.root/'formal-report-pandoc-release'
    separate_write=False
    independent_sql=None
    stale_report=None
    headers={'Authorization':'Bearer '+api.token,'Idempotency-Key':'formal-report-capacity'}
    with httpx.Client(headers=headers,timeout=150,trust_env=False) as client:
        upload=client.post(base+api.prefix+'/upload',params={'path':UNRELATED},
                           files={'file':('large.bin',payload,'application/octet-stream')})
        assert upload.status_code==200,upload.text[:400]
        (api.root/'measure-formal-report').touch()
        prior_jobs={row['id'] for row in api.call('GET',api.prefix+'/jobs')}
        if conflict:
            original=api.call('GET',api.prefix+'/raw?'+urlencode({'path':'E-report/report.md'}),raw=True)
            revised=original.decode('utf8')+'\n\nA revised source paragraph prevents a stale formal report.\n'
            (api.root/'measure-formal-report-conflict').touch()
        with ThreadPoolExecutor(max_workers=1) as workers:
            request=workers.submit(client.post,route,json={})
            try:
                wait_for_marker(entered,request)
                if api.database_url is None:
                    with sqlite3.connect(api.root/'test.sqlite',timeout=.2) as db:
                        try:
                            db.execute('BEGIN IMMEDIATE')
                            independent_sql=True
                        except sqlite3.OperationalError as error:
                            assert 'locked' in str(error).lower()
                            independent_sql=False
                        db.rollback()
                    assert independent_sql is not baseline
                if not baseline:
                    saved=client.put(base+f"/cases/{other['id']}/file",json={'path':'Ref/concurrent.txt','content':'ok\n'},timeout=15)
                    assert saved.status_code==200,saved.text[:400]
                    assert not request.done()
                    separate_write=True
            finally:
                release.touch()
            if conflict:
                try:
                    wait_for_marker(native_entered,request)
                    saved=client.put(base+api.prefix+'/file',json={'path':'E-report/report.md','content':revised},timeout=15)
                    assert saved.status_code==200,saved.text[:400]
                finally:
                    native_release.touch()
            response=request.result(timeout=150)
        if conflict:
            assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:700]
            assert {row['id'] for row in api.call('GET',api.prefix+'/jobs')}==prior_jobs
            stale_report={'status':409,'no_job_at_conflict':True,'same_key_retried':True}
            response=client.post(route,json={})
        assert response.status_code==200,response.text[:700]
        submitted=response.json()
        job=api.wait_job(submitted['job_id'],timeout=240)
        report=api.call('GET',api.prefix+'/report/results?'+urlencode({'publication_id':submitted['publication_id']}))
        html,docx=download(api,report,prefix='formal-capacity')
        verify_embedded(html,docx,(api.root/'temperature.png').read_bytes())
        sealed=job['attempt']['workspace']
        assert UNRELATED not in sealed['files']
        if conflict:
            assert sealed['files']['E-report/report.md']['sha256']==hashlib.sha256(revised.encode()).hexdigest()
            assert b'A revised source paragraph' in html
        else:
            assert any(row['kind']=='report-source' for row in sealed['publications'])
        assert client.post(route,json={}).json()['job_id']==submitted['job_id']
    traces=[json.loads(line) for line in (api.root/'formal-report-caches.jsonl').read_text('utf8').splitlines()]
    scans={}
    for row in traces:
        scan=scans.setdefault(row['scan'],{'stage':row['stage'],'bytes':0,'paths':[]})
        scan['bytes']+=row['bytes']
        scan['paths'].extend(row['paths'])
    assert {scan['stage'] for scan in scans.values()}=={'seed','pandoc'},scans
    assert all(any(UNRELATED in scan['paths'] for scan in scans.values() if scan['stage']==stage) is baseline
               for stage in ('seed','pandoc')),scans
    result={'passed':True,'baseline':baseline,'unrelated_bytes':len(payload),'case_caches':list(scans.values()),
            'independent_sqlite_write':independent_sql,'independent_case_write':separate_write,
            'actual_temperature_k':measured['mean_temperature_k'],'actual_html_docx_plot':True,
            'idempotent_job':submitted['job_id'],'source_conflict':stale_report}
    (api.root/'formal-report-capacity.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


if __name__=='__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    conflict='--conflict' in sys.argv
    if conflict:
        assert not baseline
        sys.argv.remove('--conflict')
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline,conflict=conflict),fixture_override=FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
