"""Isolated real workspace search under a held index body and request deadline."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

from index_search_io_smoke import (EDIT, FIXTURE, HELD, NEW, OLD, RUNTIME, SIZE, TIMEOUT,
                                  expire_content_verification, hit, paused_request, snapshot,
                                  writer_available)
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


def put(api, case_id, path, text):
    return api.call('PUT',f'/cases/{case_id}/file',{'path':path,'content':text})


def exercise(api, report, baseline):
    first = api.create_case('Workspace search slow source')
    report['api_process'] = api.call('GET','/fixture/search-process',api_prefix=False)
    workspace = api.call('GET','/workspaces')[0]['id']
    second = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Workspace search second'})
    api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Workspace search unindexed'})
    put(api,first['id'],HELD,'workspace search held source\n'.ljust(SIZE))
    put(api,first['id'],EDIT,OLD)
    put(api,second['id'],EDIT,OLD)
    for case in (first,second):
        api.call('POST',f'/cases/{case["id"]}/index:rebuild',{})
    path = f'/workspaces/{workspace}/search'
    first_index = api.root/'index'/f'case-{first["id"]}.sqlite'
    expire_content_verification(first_index)
    original = snapshot(first_index)
    headers = {'Authorization':'Bearer '+api.token}
    with (httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=60,trust_env=False) as search,
          httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=60,trust_env=False) as edit):
        def save_while_reading(_pending, block):
            writer = writer_available(api)
            response = edit.put(f'/cases/{first["id"]}/file',json={'path':EDIT,'content':NEW,
                'expected_sha256':hashlib.sha256(OLD.encode()).hexdigest()},timeout=20)
            return {'block':block,'writer_available':writer,'save_status':response.status_code,
                    'save_reply':response.json()}

        response, evidence, trace = paused_request(api,search,path,OLD,'edit',save_while_reading)
        evidence.update(search_status=response.status_code,search_reply=response.json(),trace=trace)
        report['concurrent_save'] = evidence
        assert evidence['writer_available'] is not baseline, evidence
        assert evidence['block']['deadline_active'] is not baseline, evidence
        if baseline:
            assert evidence['save_status']==409 and evidence['save_reply']['detail']['code']=='storage_quota_busy', evidence
            assert response.status_code==200 and [h['case_id'] for h in response.json()['hits']]==[first['id'],second['id']]
            return

        assert evidence['save_status']==200 and response.status_code==409, evidence
        assert evidence['search_reply']['detail']['code']=='index_source_changed', evidence
        assert snapshot(first_index)==original
        fresh = hit(search,path,NEW)
        assert fresh['ready'] and (fresh['total_cases'],fresh['indexed_cases'])==(3,2), fresh
        assert [row['case_id'] for row in fresh['hits']]==[first['id']], fresh
        assert [row['case_id'] for row in hit(search,path,OLD)['hits']]==[second['id']]
        by_name = hit(search,path,'index-version.txt')
        assert [row['case_id'] for row in by_name['hits']]==[first['id'],second['id']], by_name
        assert [row['case_name'] for row in by_name['hits']]==[first['name'],second['name']], by_name
        assert all(row['via']=='name' for row in by_name['hits'])
        assert search.get(path,params={'q':'index-version.txt','limit':1}).json()['hits']==by_name['hits'][:1]
        report['workspace_shape'] = by_name

        expire_content_verification(first_index)
        before_expiry = snapshot(first_index)

        def wait_expired(pending, block):
            assert block['deadline_active'] and 0 < block['remaining'] <= TIMEOUT, block
            time.sleep(block['remaining']+.20)
            assert not pending.done(), 'Response completed before the active reader drained'
            return True

        response, pending, trace = paused_request(api,search,path,NEW,'expiry',wait_expired)
        report['expiry'] = {'drained_first':pending,'status':response.status_code,'trace':trace}
        assert response.status_code==504 and response.json()['detail']['code']=='workspace_search_timeout',response.text
        assert trace['expired_at_release'] and not trace['io_started_after_expiry'],trace
        assert snapshot(first_index)==before_expiry and hit(search,path,NEW)==fresh

        email = uuid.uuid4().hex+'@example.com'
        viewer = api.call('POST','/auth/register',{'email':email,'name':'Workspace search viewer',
                          'password':'isolated-test-123'})['token']
        viewer_header = {'Authorization':'Bearer '+viewer}
        assert search.get(path,params={'q':NEW},headers=viewer_header).status_code==404
        org = api.call('GET','/org/mine')['org']['id']
        api.call('POST',f'/org/{org}/members',{'email':email,'role':'viewer'},expect=201)
        api.call('POST',f'/org/{org}/invitation:accept',{},extra_headers=viewer_header)
        share_route = f'/workspaces/{workspace}/sharing'
        share = {'org_id':org,'role':'viewer','enabled':True}
        api.call('PUT',share_route,share)
        observed = search.get(path,params={'q':NEW},headers=viewer_header)
        assert observed.status_code==200 and observed.json()==fresh
        api.call('PUT',share_route,{**share,'enabled':False})
        assert search.get(path,params={'q':NEW},headers=viewer_header).status_code==404
        report['viewer_statuses'] = [404,200,404]


def run(backend, baseline):
    from app.services import process_identity_native

    api = IsolatedServer('workspace-search-io-'+backend,app='workspace_search_fixture:app')
    evidence = api.root/'workspace-search-io.json'
    report = {'passed':False,'baseline':baseline,'backend':backend,'api_port':api.port,
              'real_model_calls':0,'mock_llm_calls':0}
    context = ObjectStore(executable=RUNTIME,volume_max=24,authenticated=True) if backend=='s3' else nullcontext()
    print('WORKSPACE_SEARCH_IO_EVIDENCE',api.root,flush=True)
    try:
        with context as store:
            configuration = store.settings() if store is not None else {}
            if store is not None:
                report['store_receipt'] = str(store.root/'store.json')
            temporary = api.root/'native-temp'
            temporary.mkdir()
            configuration.update(TEMP=str(temporary),TMP=str(temporary),TMPDIR=str(temporary),
                                 VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS=str(TIMEOUT))
            module = api.root/'workspace_search_fixture.py'
            try:
                module.write_text('import os\nos.environ.update('+repr(configuration)+')\n'+FIXTURE,encoding='utf8')
                with api:
                    exercise(api,report,baseline)
            finally:
                module.unlink(missing_ok=True)
        if store is not None:
            report['store_exit'] = store.proc.poll()
            assert report['store_exit']==0
        report['api_exit'] = api.proc.poll()
        assert report['api_exit'] is not None
        api.proc = None
        until = time.monotonic()+5
        while True:
            proof = report['api_identity_after'] = process_identity_native.probe(report['api_process'])
            if proof['state']=='exited' or time.monotonic()>=until:
                break
            time.sleep(.05)
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__,'message':str(error)[:1600]}
        raise
    finally:
        evidence.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('WORKSPACE_SEARCH_IO_PASS',evidence,flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    baseline = os.environ.get('VIBEFLOW_WORKSPACE_SEARCH_IO_BASELINE')=='1'
    run('fs',baseline)
    if not baseline:
        run('s3',False)
