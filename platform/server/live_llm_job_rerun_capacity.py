"""Measure current-input reruns after real S3 upload acknowledgement."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as PendingResponse
from functools import partial
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from urllib.parse import urlencode

import httpx

from live_llm_job_repeat_acceptance import main, put
from live_llm_job_retry_capacity import FIXTURE, SOURCE, wait_for


RECOVERY_OBSERVATION=r'''
from pathlib import Path
from app.services import attempt_seal_journal
original_recover_pending=attempt_seal_journal.recover_pending
async def observe_recovery(*args,**kwargs):
    result=await original_recover_pending(*args,**kwargs)
    required=[row for row in result if row['status']=='recovery_required']
    if required:
        import json
        path=Path(__file__).with_name('actual-seal-recovery-blocked.json')
        path.write_text(json.dumps(required,ensure_ascii=False,indent=2),encoding='utf8')
    return result
attempt_seal_journal.recover_pending=observe_recovery
'''


async def add_intervening_event(api):
    import re

    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.services.durable_events import append_event

    url=make_url(api.database_url)
    assert url.host in {'127.0.0.1','localhost','::1'} and re.fullmatch(r'tmp_vf_repeat_[0-9a-f]{32}',url.database)
    engine=create_async_engine(url)
    try:
        async with AsyncSession(engine) as db:
            event=await append_event(db,api.case_id,'verification.rerun_interleaving',{'fixture':'rerun_capacity'})
            sequence=event.seq
            await db.commit()
            return sequence
    finally:
        await engine.dispose()


def scenario(api, *, baseline, intervening_event=False, store=None, **_):
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response

    assert store is not None
    wait_for_s3_response(api,store)
    initial=b'original execution input\n'
    current=b'current rerun input\n'+b'r'*(12*1024*1024)
    current_sha=hashlib.sha256(current).hexdigest()
    initial_sha=hashlib.sha256(initial).hexdigest()
    proof=api.root/'actual-rerun-proof.log'
    code=("from pathlib import Path\nimport hashlib\n"
          "with Path('Ref/retry-source.bin').open('rb') as stream:\n"
          "    digest=hashlib.file_digest(stream,'sha256').hexdigest()\nprint(digest,flush=True)\n"
          f"with Path({str(proof)!r}).open('a',encoding='utf8') as stream: stream.write(digest+'\\n')\n")
    put(api,'Ref/rerun.py',code)
    put(api,SOURCE,initial.decode())
    put(api,'Ref/not-selected.txt','this file is outside the explicit input selection')
    case=api.call('GET',api.prefix)
    other=api.call('POST',f"/workspaces/{case['workspace_id']}/cases",{'name':'Concurrent rerun writer'})
    base=api.base+'/api/v1'
    headers={'Authorization':'Bearer '+api.token}
    key='actual-bounded-rerun'
    marker=api.root/'retry-capacity-entered.json'
    release=api.root/'retry-capacity-release'
    command={'command':'python Ref/rerun.py'}
    if not intervening_event:
        command['input_paths']=['Ref/rerun.py',SOURCE]
    first=api.call('POST',api.prefix+'/jobs',command)
    old=api.wait_job(first['id'])
    assert initial_sha in old['log_backlog'] and old['attempt']['process_stop']['verified']
    route=base+f"/jobs/{first['id']}:repeat"
    options_path=f"/jobs/{first['id']}/operations"

    def selection():
        option=api.call('GET',options_path)
        assert option['rerun']['available']
        return {'action':'rerun',**{name:option[name] for name in (
            'source_attempt_id','source_version','expected_revision','expected_event_sequence')}}

    sqlite_writer=None
    other_case=False
    intervening_sequence=None
    concurrent_replay=False
    with httpx.Client(headers=headers,timeout=155,trust_env=False) as client:
        stale=selection()
        written=client.post(base+api.prefix+'/upload',params={'path':SOURCE},
                            files={'file':('live.bin',current,'application/octet-stream')})
        assert written.status_code==200,written.text[:550]
        denied=client.post(route,json=stale,headers={'Idempotency-Key':key})
        assert denied.status_code==409 and denied.json()['detail']['code']=='input_stale',denied.text[:650]
        assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}=={first['id']}
        confirmed=selection()
        assert confirmed['expected_event_sequence']>stale['expected_event_sequence']
        (api.root/'retry-capacity-arm').touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(client.post,route,json=confirmed,headers={'Idempotency-Key':key})
            duplicate=None
            try:
                wait_for(marker,pending)
                probe=json.loads(marker.read_text(encoding='utf8'))
                assert probe['bytes']==len(current) and len(probe['copies'])==(3 if baseline else 1),probe
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
                    edit=client.put(base+api.prefix+'/file',json={'path':'Ref/not-selected.txt','content':'blocked\n'},timeout=15)
                    assert edit.status_code==409 and edit.json()['detail']['code']=='file_sync_busy',edit.text[:550]
                    saved=client.put(base+f"/cases/{other['id']}/file",json={'path':'Ref/parallel.txt','content':'saved\n'},timeout=15)
                    assert saved.status_code==200 and not pending.done(),saved.text[:550]
                    other_case=True
                    if intervening_event:
                        intervening_sequence=asyncio.run(add_intervening_event(api))
                        assert intervening_sequence==confirmed['expected_event_sequence']+1
                    else:
                        duplicate=pool.submit(client.post,route,json=confirmed,headers={'Idempotency-Key':key})
                        try:
                            early=duplicate.result(timeout=.5)
                        except PendingResponse:
                            pass
                        else:
                            raise AssertionError(f'Same-key replay returned before commit: {early.status_code} {early.text[:500]}')
            finally:
                release.touch()
            response=pending.result(timeout=155)
            if duplicate is not None:
                shared=duplicate.result(timeout=155)
                assert shared.status_code==202 and not shared.json()['created'],shared.text[:750]
                assert shared.json()['id']==response.json()['id']
                concurrent_replay=True
        if intervening_event:
            assert response.status_code==409 and response.json()['detail']['code']=='input_stale',response.text[:750]
            assert {item['id'] for item in api.call('GET',api.prefix+'/jobs')}=={first['id']}
            rejected_attempt=probe['id']
            confirmed=selection()
            assert confirmed['expected_event_sequence']>intervening_sequence
            marker.unlink()
            response=client.post(route,json=confirmed,headers={'Idempotency-Key':key})
            assert response.status_code==202,response.text[:750]
            probe=json.loads(marker.read_text(encoding='utf8'))
            assert probe['id']!=rejected_attempt
        assert response.status_code==202,response.text[:750]
        result=response.json()
        done=api.wait_job(result['id'],timeout=240)
        assert done['attempt']['id']==probe['id'] and done['attempt']['root_job_id']==result['id']
        assert done['attempt']['number']==1 and done['attempt']['retry_of_attempt_id'] is None
        files=done['attempt']['workspace']['files']
        selected=set(old['attempt']['workspace']['files']) if intervening_event else {'Ref/rerun.py',SOURCE}
        assert not intervening_event or 'Ref/not-selected.txt' in selected
        assert set(files)==selected and files[SOURCE]['sha256']==current_sha,sorted(files)
        assert old['attempt']['workspace']['files'][SOURCE]['sha256']==initial_sha
        assert proof.read_text(encoding='utf8').splitlines()==[initial_sha,current_sha]
        replay=client.post(route,json=confirmed,headers={'Idempotency-Key':key})
        assert replay.status_code==202 and replay.json()['id']==result['id'] and not replay.json()['created']
        live=api.call('GET',api.prefix+'/raw?'+urlencode({'path':SOURCE}),raw=True)
        assert hashlib.sha256(live).hexdigest()==current_sha
    report={'passed':True,'baseline':baseline,'measurement':probe,'sqlite_writer':sqlite_writer,'separate_case_saved':other_case,
            'previous_job':first['id'],'rerun_job':result['id'],'old_bytes_retained':True,'new_bytes_executed_once':True,
            'fresh_confirmation_required':True,'selection':'case' if intervening_event else 'explicit','same_key_replayed':True,
            'same_key_concurrent_replay':concurrent_replay,'intervening_event_sequence':intervening_sequence}
    (api.root/'job-rerun-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


def registered_scenario(api, *, store, **_):
    from app.services.ppt_handler import CONFIG
    from live_llm_document_repeat_acceptance import png_bytes, upload, verify_ppt
    from live_llm_engineering_report_schema_acceptance import wait_for_s3_response

    wait_for_s3_response(api,store)

    def rerun(identifier,params=None):
        key=uuid.uuid4().hex
        options=api.call('GET',f'/jobs/{identifier}/operations')
        assert options['rerun']['available']
        data={'action':'rerun',**{name:options[name] for name in (
            'source_attempt_id','source_version','expected_revision','expected_event_sequence')}}
        if params is not None:data['parameters']=params
        result=api.call('POST',f'/jobs/{identifier}:repeat',data,expect=202,extra_headers={'Idempotency-Key':key})
        done=api.wait_job(result['id'])
        assert done['attempt']['number']==1 and done['attempt']['root_job_id']==result['id']
        assert done['attempt']['outputs']['publication']['status']=='committed'
        return done['attempt']

    def update(path,content):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            result=api.call('PUT',api.prefix+'/file',{'path':path,'content':content},expect=(200,409))
            detail=result.get('detail')
            if not detail:return
            assert detail['code'] in {'file_sync_busy','storage_quota_busy','validation_busy'},detail
            time.sleep(.2)
        raise TimeoutError('The actual file write did not receive admission')

    image='Ref/selected.png'
    before,after=png_bytes('orange'),png_bytes('blue')
    upload(api,image,before,'image/png')
    ppt={'output':'E-report/original.pptx','title':'Frozen document','template':None,
         'slides':[{'title':'Frozen section','bullets':['Actual editable slide'],'image':{'path':image}}]}
    source=api.call('POST',api.prefix+'/ppt:build',ppt,expect=200)
    old=api.wait_job(source['job_id'])['attempt']
    original=api.call('GET',api.prefix+'/raw?'+urlencode({'path':ppt['output']}),raw=True)
    verify_ppt(original,before)
    upload(api,image,after,'image/png')
    fresh={**ppt,'output':'E-report/current.pptx'}
    attempt=rerun(source['job_id'],fresh)
    verify_ppt(api.call('GET',api.prefix+'/raw?'+urlencode({'path':fresh['output']}),raw=True),after)
    assert api.call('GET',api.prefix+'/raw?'+urlencode({'path':ppt['output']}),raw=True)==original
    assert old['workspace']['files'][CONFIG]['sha256']!=attempt['workspace']['files'][CONFIG]['sha256']
    assert attempt['workspace']['files'][image]['sha256']==hashlib.sha256(after).hexdigest()

    api.create_case('Actual typed command rerun')
    update('Ref/source.txt','original file\n')
    update('Ref/read.py',"from pathlib import Path\nPath('D-post/result.txt').write_bytes(Path('Ref/source.txt').read_bytes())\n")
    typed=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':'python Ref/read.py'})
    api.wait_job(typed['id'])
    update('Ref/source.txt','current file\n')
    typed_rerun=rerun(typed['id'])
    assert typed_rerun['handler']['key']=='command.convert'
    assert api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)==b'current file\n'
    report={'passed':True,'ppt_generated_input_verified':True,'old_actual_ppt_preserved':True,
            'new_actual_ppt_parsed':True,'typed_command_current_input_executed':True}
    (api.root/'job-rerun-registered.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    intervening_event='--intervening-event' in sys.argv
    if intervening_event:
        assert '--postgres' in sys.argv and not baseline
        sys.argv.remove('--intervening-event')
    registered='--registered' in sys.argv
    if registered:
        assert not baseline and not intervening_event
        sys.argv.remove('--registered')
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    selected=registered_scenario if registered else partial(scenario,baseline=baseline,intervening_event=intervening_event)
    asyncio.run(main(selected_override=selected,fixture_override=RECOVERY_OBSERVATION if registered else FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
