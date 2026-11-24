"""Exercise direct and scheduled engineering confirmation with real storage."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import httpx

from case_workflow_thermal_fixture import GEOMETRY as THERMAL_GEOMETRY, RUN, case_dictionary, thermal_files
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


TIMELINE = '0-caseDict/case-events.jsonl'
DICTIONARY = '0-caseDict/caseDict'
PROVENANCE = '0-caseDict/provenance.jsonl'
BYTES = 40 * 1024 * 1024
GEOMETRY = '''import cadquery as cq

def build_geometry(ctx):
    return cq.Workplane('XY').box(1, 1, 1, centered=(False, False, False))
'''
APP = '''
from contextlib import closing
import asyncio
import hashlib
import json
from pathlib import Path
import time

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services.case_events import CASE_EVENTS_PATH
from app.services.file_read import FileRead
from app.services.request_deadline import current

root = Path(__file__).parent
original = FileRead.chunks
armed = {}

def observe(reader, *args, **kwargs):
    prefix = armed.get('prefix')
    path = CASE_EVENTS_PATH
    selected = prefix and (reader.location['Key'] == storage._s3_key(prefix, path) if reader.remote
                           else reader.path == storage._fs_path(prefix, path))
    with closing(original(reader,*args,**kwargs)) as chunks:
        for chunk in chunks:
            if selected:
                label = armed['label']
                witness = root / (label+'-history.json')
                if not witness.exists():
                    record = witness.with_suffix('.partial')
                    record.write_text(json.dumps({'size':reader.size,'first_read':len(chunk)}),encoding='utf8')
                    record.replace(witness)
                    limit = time.monotonic()+22
                    while not (root/(label+'-release')).exists():
                        if time.monotonic()>=limit:
                            raise TimeoutError('Timed out waiting for independent writer')
                        time.sleep(.02)
            yield chunk

FileRead.chunks = observe

original_commit = AsyncSession.commit

async def commit(self):
    installed = bool(self.info.get('local_file_transactions'))
    await original_commit(self)
    request_deadline = current()
    trigger = root/'expire-after-commit'
    if installed and request_deadline is not None and trigger.is_file():
        trigger.unlink()
        (root/'actual-commit-before-deadline.json').write_text(
            json.dumps({'remaining_seconds':request_deadline.remaining()}),encoding='utf8')
        await asyncio.sleep(request_deadline.remaining()+.1)

AsyncSession.commit = commit

@app.post('/fixture/stage-history/{case_id}')
async def seed(case_id:int, label:str, user=Depends(get_current_user), db=Depends(get_db)):
    assert label in {'direct','scheduled'}
    case = await get_owned_case(case_id,user,db)
    start = b'{"data":"'
    end = b'"}'+bytes((10,))
    size = 40*1024*1024
    data = start+b'x'*(size-len(start)-len(end))+end
    storage.put_bytes(case.s3_prefix,CASE_EVENTS_PATH,data)
    armed.update(prefix=case.s3_prefix,label=label)
    return {'size':size,'sha256':hashlib.sha256(data).hexdigest()}
'''


def dig(data):
    return hashlib.sha256(data).hexdigest()


def raw(api, prefix, path, *, expect=200):
    return api.call('GET',prefix+'/raw?path='+path,raw=True,expect=expect)


def append_only(api, prefix, seed, revision, state):
    timeline = raw(api,prefix,TIMELINE)
    assert dig(timeline[:BYTES])==seed['sha256']
    additions = timeline[BYTES:].splitlines()
    assert len(additions)==1
    event = json.loads(additions[0])
    assert event['type']=='caseDict.updated' and event['source']=='workflow-advance',event
    assert event['revision']==revision and event['workflow_state']==state,event
    provenance = raw(api,prefix,PROVENANCE)
    latest = json.loads(provenance.splitlines()[-1])
    dictionary_sha256 = dig(raw(api,prefix,DICTIONARY))
    assert latest['revision']==revision and latest['casedict_sha256']==dictionary_sha256,latest
    return {'timeline_sha256':dig(timeline),'dictionary_sha256':dictionary_sha256,
            'provenance_sha256':dig(provenance)}


def advance(api, prefix, request):
    headers = {'Authorization':'Bearer '+api.token}
    with httpx.Client(base_url=api.base, headers=headers, trust_env=False, timeout=90) as client:
        result = client.post('/api/v1'+prefix+'/caseDict:advance',json=request)
        assert result.status_code==200, result.text
        return result.json()


def confirm_while_reading(api, case_id, other_id, label, request):
    prefix, other = f'/cases/{case_id}', f'/cases/{other_id}'
    seed = api.call('POST',f'/fixture/stage-history/{case_id}?label={label}',api_prefix=False)
    assert seed['size']==BYTES
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(advance,api,prefix,request)
        try:
            witness = api.root/(label+'-history.json')
            limit = time.monotonic()+20
            while not witness.is_file():
                if pending.done():
                    raise AssertionError(pending.result())
                if time.monotonic()>limit:
                    raise TimeoutError('No physical timeline read during stage confirmation')
                time.sleep(.025)
            observed = json.loads(witness.read_text(encoding='utf8'))
            assert observed['size']==BYTES and 0<observed['first_read']<BYTES and not pending.done()
            with sqlite3.connect(api.root/'test.sqlite',timeout=.3) as db:
                db.execute('BEGIN IMMEDIATE')
            body = {'path':f'Ref/{label}.txt','content':'independent case committed during history transfer'}
            api.call('PUT',other+'/file',body)
            assert raw(api,other,body['path'])==body['content'].encode() and not pending.done()
        finally:
            (api.root/(label+'-release')).touch()
        result = pending.result(95)
    stage, revision = request['state'],request['expected_revision']+1
    assert result['workflow_state']==stage and result['dict_revision']==revision,result
    mirror = api.call('GET',prefix+'/caseDict')
    assert mirror['workflow_state']==stage and mirror['dict_revision']==revision,mirror
    return {**observed,**append_only(api,prefix,seed,revision,stage),'another_case_committed':True,'receipt':result}


def settled_job(api, identifier, timeout=150):
    limit = time.monotonic()+timeout
    while time.monotonic()<limit:
        job = api.call('GET',f'/jobs/{identifier}')
        if job['status']=='succeeded' and not job['completion_pending']:
            assert job['attempt']['process_stop']['verified'],job
            return job
        assert job['status'] in {'queued','running','succeeded'},job
        time.sleep(.2)
    raise TimeoutError(f'Native job {identifier} did not finish publishing')


def awaiting(api, identifier, state):
    producer_key = {'B':'geometry','C':'mesh','D':'solve','E':'postprocess'}[state]
    limit = time.monotonic()+150
    while time.monotonic()<limit:
        flow = api.call('GET','/workflow-runs/'+identifier)
        for step in flow['steps']:
            if step['key']==f'confirm_{state}' and step['status']=='awaiting_confirmation':
                producer = next(item for item in flow['steps'] if item['key']==producer_key)
                for attempt in producer['attempts']:
                    settled_job(api,attempt['job_id'])
                return step
        assert flow['status'] not in {'failed','cancelled'},flow
        time.sleep(.2)
    raise TimeoutError(f'Native workflow did not arm {state} confirmation')


def request_confirmation(step):
    return {'state':step['confirmation_state'],'confirmed':True,
            'expected_revision':step['confirmation_revision'],
            'workflow_step_id':step['id'],'expected_step_version':step['version']}


def verify(backend, *, full_chain):
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    backend_context = ObjectStore(executable=binary,volume_max=9,authenticated=True) if backend=='s3' else nullcontext()
    with backend_context as store:
        api = IsolatedServer('casedict-stage-capacity-'+backend,app='casedict_stage_fixture:app')
        fixture = api.root/'casedict_stage_fixture.py'
        setup = 'import os\nos.environ.update('+repr(store.settings())+')\n' if store else ''
        fixture.write_text(setup+APP,encoding='utf8')
        try:
            with api:
                direct = api.create_case('Direct confirmation')
                workspace_id = api.call('GET','/workspaces')[0]['id']
                group = f'/workspaces/{workspace_id}/cases'
                workflow = api.call('POST',group,{'name':'Scheduled confirmation'})
                other = api.call('POST',group,{'name':'Concurrent independent writer'})
                prefix = f'/cases/{other["id"]}'
                before = {path:dig(raw(api,prefix,path,expect=(200,404)))
                          for path in (DICTIONARY,TIMELINE,PROVENANCE)}
                blocked = api.call('POST',prefix+'/caseDict:advance',{
                    'state':'B','expected_revision':0,'confirmed':True},expect=409)
                assert blocked['detail'] and {path:dig(raw(api,prefix,path,expect=(200,404))) for path in before}==before
                assert api.call('GET',prefix+'/caseDict')['dict_revision']==0
                direct_result = confirm_while_reading(api,direct['id'],other['id'],'direct',
                    {'state':'V','expected_revision':0,'confirmed':True})
                same = api.call('POST',api.prefix+'/caseDict:advance',{
                    'state':'V','expected_revision':1,'confirmed':True})
                assert same['dict_revision']==1
                assert dig(raw(api,api.prefix,TIMELINE))==direct_result['timeline_sha256']
                api.call('POST',api.prefix+'/caseDict:advance',{
                    'state':'A','expected_revision':0,'confirmed':True},expect=409)

                workflow_prefix = f'/cases/{workflow["id"]}'
                if full_chain:
                    dictionary = api.call('GET',workflow_prefix+'/caseDict')
                    api.call('PUT',workflow_prefix+'/caseDict',{
                        'content':case_dictionary(dictionary['content']),'expected_revision':dictionary['dict_revision']})
                    for path,content in thermal_files().items():
                        api.call('PUT',workflow_prefix+'/file',{'path':path,'content':content})
                    geometry = THERMAL_GEOMETRY
                    target, mode = 'report','export'
                else:
                    geometry = GEOMETRY
                    target, mode = 'geometry','preview'
                job_result = api.call('PUT',workflow_prefix+'/file',{'path':'A-geo/geometry.py','content':geometry})
                preview = settled_job(api,job_result['auto_preview_job'])
                assert preview['status']=='succeeded',preview
                flow = api.call('POST',workflow_prefix+'/workflow-runs',{
                    'target':target,'run_dir':RUN,'expected_revision':api.call('GET',workflow_prefix+'/caseDict')['dict_revision'],
                    'parameters':{'geometry_mode':mode,'report_mode':'compose'},
                    'budget':{'timeout_seconds':180,'max_log_bytes':4*1024*1024}})
                step = awaiting(api,flow['id'],'B')
                request = request_confirmation(step)
                scheduled = confirm_while_reading(api,workflow['id'],other['id'],'scheduled',request)
                assert scheduled['receipt']['workflow_dispatch']['status']=='dispatched',scheduled
                receipts = [scheduled['receipt']]
                for stage in ('C','D','E') if full_chain else ():
                    later_step = awaiting(api,flow['id'],stage)
                    if stage=='E' and backend=='s3':
                        (api.root/'expire-after-commit').touch()
                    result = advance(api,workflow_prefix,request_confirmation(later_step))
                    assert result['workflow_state']==stage and result['dict_revision']==len(receipts)+request['expected_revision']+1
                    assert result['workflow_dispatch']['status']=='dispatched',result
                    receipts.append(result)
                limit = time.monotonic()+150
                while True:
                    final = api.call('GET','/workflow-runs/'+flow['id'])
                    if final['status']=='succeeded':
                        break
                    assert time.monotonic()<limit and final['status'] not in {'failed','cancelled'},final
                    time.sleep(.2)
                saved = next(item for item in final['steps'] if item['id']==step['id'])
                assert saved['status']=='succeeded' and saved['version']==step['version']+1,final
                api.call('POST',workflow_prefix+'/caseDict:advance',request,expect=409)
                jobs = api.call('GET',workflow_prefix+'/jobs')
                report = None
                if full_chain:
                    by_type = {job['type'] for job in jobs}
                    assert {'geometry','mesh','solve','postprocess','convert'} <= by_type,by_type
                    jobs = [settled_job(api,job['id']) for job in jobs]
                    result = api.call('GET',workflow_prefix+'/report/results')['publication']
                    html = api.call('GET',workflow_prefix+'/report/artifact?publication_id='+result['id']+
                                    '&path=E-report/build/report.html',raw=True)
                    assert b'mean.T@1' in html
                    report = {'publication_id':result['id'],'report_sha256':dig(html)}
                    if backend=='s3':
                        witness = api.root/'actual-commit-before-deadline.json'
                        assert witness.is_file() and json.loads(witness.read_text(encoding='utf8'))['remaining_seconds']>0
                        report['postcommit_deadline_returned_success']=True
                return {'backend':backend,'root':str(api.root),'direct':direct_result,'scheduled':scheduled,
                        'workflow_id':flow['id'],'native_geometry_preview':preview['id'],
                        'confirmed_stages':[receipt['workflow_state'] for receipt in receipts],
                        'native_jobs':[{'id':job['id'],'type':job['type'],'status':job['status']} for job in jobs],
                        'report':report,
                        'missing_evidence_rollback_and_stale_confirmation_rejected':True}
        finally:
            fixture.unlink(missing_ok=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=('fs','s3','all'),default='all')
    parser.add_argument('--full-chain',action='store_true')
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    report = {'passed':False,'runs':[],'actual_model_calls':0,'simulated_model_calls':0}
    try:
        for backend in ('fs','s3') if args.backend=='all' else (args.backend,):
            report['runs'].append(verify(backend,full_chain=args.full_chain))
            print('CASEDICT_STAGE_CAPACITY_PASS',backend,flush=True)
        report['passed'] = True
    finally:
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
