"""Real FS case and workspace copies during final large-file transfers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx

from case_copy_source_lock_smoke import EDIT, LARGE, digest_response, read_observation, seed, writer_available
from testsupport import IsolatedServer, SERVER

FIXTURE = r'''
import os
import time
import json
from pathlib import Path
from contextvars import ContextVar
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.services import case_provision, node_storage_budget, process_identity_native
from app.services.case_source import SourceFile
from app.services.request_deadline import current

root = Path(__file__).parent
phase = ContextVar('copy_final_phase',default=None)
copying = ContextVar('copy_final_active',default=False)
original_copy = SourceFile.copy_into
original_open = Path.open
original_witness = case_provision.record_witness

class ObservedBody:
    def __init__(self, body):
        self.body = body
        self.observed = False
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self.body,name)

    def __enter__(self):
        self.body.__enter__()
        return self

    def __exit__(self, *args):
        return self.body.__exit__(*args)

    def read(self, size):
        data = self.body.read(size)
        self.reads += 1
        if phase.get()=='crash-preparation' and self.reads==2 and data:
            (root/'prepare-exited').touch()
            os._exit(85)
        if data and not self.observed:
            self.observed = True
            name = phase.get()
            if name.startswith('crash-'):
                return data
            path = root/(name+'.json')
            temporary = path.with_suffix('.new')
            details = {'bytes':len(data),'reserved':node_storage_budget.observe()['reserved_bytes']}
            if current() is not None:
                details['remaining'] = current().remaining()
            temporary.write_text(json.dumps(details), encoding='utf8')
            temporary.replace(path)
            until = time.monotonic()+30
            while not (root/('release-'+name)).exists():
                if time.monotonic()>=until:
                    raise TimeoutError('Actual final-copy read did not get released')
                time.sleep(.02)
        return data

def observed_copy(self, destination):
    token = copying.set(phase.get() is not None and self.path=='Ref/archive-large.bin')
    try:
        return original_copy(self,destination)
    finally:
        copying.reset(token)

def observed_open(self, *args, **kwargs):
    stream = original_open(self,*args,**kwargs)
    return ObservedBody(stream) if copying.get() and args==('rb',) and self.name=='archive-large.bin' else stream

SourceFile.copy_into = observed_copy
Path.open = observed_open

async def witness(db, case, plan):
    if phase.get()=='crash-install':
        (root/'uncommitted-install.json').write_text(json.dumps(plan),encoding='utf8')
        os._exit(86)
    await original_witness(db,case,plan)
case_provision.record_witness = witness

@app.get('/fixture/final-copy-owner')
def observed_owner(user=Depends(get_current_user)):
    return {'identity':process_identity_native.observe(os.getpid()), 'reserved':node_storage_budget.observe()['reserved_bytes']}

class Observe:
    def __init__(self,app):
        self.app=app

    async def __call__(self,scope,receive,send):
        requested=dict(scope.get('headers',())).get(b'x-fixture-final-copy')
        if requested is None or not scope.get('path','').endswith(':copy'):
            return await self.app(scope,receive,send)
        token=phase.set(requested.decode('ascii'))
        settings=get_settings()
        previous=settings.case_copy_timeout_seconds if phase.get()=='deadline' else None
        if previous is not None:
            settings.case_copy_timeout_seconds=4
        try:
            return await self.app(scope,receive,send)
        finally:
            phase.reset(token)
            if previous is not None:
                settings.case_copy_timeout_seconds=previous

app=Observe(app)
'''


def interrupt_and_restart(api, proof, route, body, email, mode, code):
    from app.services import process_identity_native

    birth=api.call('GET',proof,api_prefix=False)['identity']
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      trust_env=False,timeout=90) as client:
        try:
            client.post(route,json=body,headers={'X-Fixture-Final-Copy':mode})
        except (httpx.RemoteProtocolError,httpx.ReadError):
            pass
        else:
            raise AssertionError('The stopped process returned a response')
    assert api.proc.wait(timeout=15)==code
    for _ in range(100):
        status=process_identity_native.probe(birth)
        if status['state']=='exited':
            break
        time.sleep(.05)
    assert status['state']=='exited',status
    api.__exit__(None,None,None)
    api.__enter__()
    api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
    return {'birth':birth,'exit_code':code}


def verify(baseline):
    api=IsolatedServer('case-copy-final-fs',app='case_copy_final_fixture:app')
    (api.root/'case_copy_final_fixture.py').write_text(FIXTURE,encoding='utf8')
    print('CASE_COPY_FINAL_EVIDENCE '+str(api.root),flush=True)
    report={'passed':False,'baseline':baseline,'storage':'fs','database':'sqlite','api_port':api.port,
            'actual_model_calls':0,'mock_llm_calls':0,'checks':[]}
    try:
        with api:
            source=api.create_case('Actual final copy source')
            email=api.call('GET','/auth/me')['email']
            workspace=api.call('GET','/workspaces')[0]
            proof='/fixture/final-copy-owner'
            report['api_process']=api.call('GET',proof,api_prefix=False)['identity']
            _, expected=seed(api,None)
            report['source']=expected
            initial,updated='old source text','new source text'
            api.put(EDIT,initial)
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                              trust_env=False,timeout=100) as client:
                def pause_copy(label, endpoint, body, intervening):
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        pending=worker.submit(client.post,endpoint,json=body,headers={'X-Fixture-Final-Copy':label})
                        try:
                            stage=read_observation(api,label+'.json')
                            assert 0<stage['bytes']<=1024*1024 and stage['reserved']>=expected['size'] and not pending.done()
                            writer=writer_available(api)
                            operation=intervening()
                        finally:
                            (api.root/('release-'+label)).touch()
                        result=pending.result(timeout=90)
                    assert writer is (not baseline)
                    report['checks'].append({'name':label,'writer_available':writer,'copy_http':result.status_code,
                        'concurrent_http':operation.status_code,'first_block':stage['bytes'],'node_reserved':stage['reserved']})
                    return result,operation

                case_route=api.prefix+':copy'
                case_body={'name':'Public final copy target'}
                edit={'path':EDIT,'content':updated,'expected_sha256':hashlib.sha256(initial.encode()).hexdigest()}
                copied,save=pause_copy('case-copy',case_route,case_body,
                    lambda:client.put(api.prefix+'/file',json=edit,timeout=20))
                if baseline:
                    assert copied.status_code==200 and save.status_code==409,(copied.text,save.text)
                    assert client.get(f'/cases/{copied.json()["id"]}/raw',params={'path':EDIT}).text==initial
                else:
                    cases=f'/workspaces/{workspace["id"]}/cases'
                    assert copied.status_code==409 and save.status_code==200,(copied.text,save.text)
                    assert [row['id'] for row in api.call('GET',cases)]==[source['id']]
                    retry=client.post(case_route,json=case_body)
                    assert retry.status_code==200,retry.text
                    clone=retry.json()['id']
                    assert client.get(f'/cases/{clone}/raw',params={'path':EDIT}).text==updated
                    assert digest_response(client,clone,LARGE)==expected
                    report['checks'].append({'name':'case-retry','copy_http':200,'bytes_verified':expected['size']})

                    workspace_route=f'/workspaces/{workspace["id"]}:copy'
                    workspace_body={'name':'Public workspace final copy target','include_cases':True}
                    stale,late=pause_copy('workspace-copy',workspace_route,workspace_body,
                        lambda:client.post(cases,json={'name':'Case added during final copy'},timeout=20))
                    assert stale.status_code==409 and late.status_code==200,(stale.text,late.text)
                    assert workspace_body['name'] not in {row['name'] for row in api.call('GET','/workspaces')}
                    repeated=client.post(workspace_route,json=workspace_body)
                    assert repeated.status_code==200,repeated.text
                    found=api.call('GET',f'/workspaces/{repeated.json()["id"]}/cases')
                    names={row['name']:row['id'] for row in found}
                    assert set(names)=={source['name'],case_body['name'],'Case added during final copy'},names
                    assert digest_response(client,names[source['name']],LARGE)==expected
                    report['checks'].append({'name':'workspace-retry','copy_http':200,'case_count':len(names)})
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        pending=worker.submit(client.post,case_route,json={'name':'Timed out private copy'},
                                              headers={'X-Fixture-Final-Copy':'deadline'})
                        try:
                            pause=read_observation(api,'deadline.json')
                            time.sleep(pause['remaining']+.15)
                            assert not pending.done()
                        finally:
                            (api.root/'release-deadline').touch()
                        expired=pending.result(timeout=30)
                    assert expired.status_code==504 and expired.json()['detail']['code']=='case_copy_timeout',expired.text
                    assert 'Timed out private copy' not in {row['name'] for row in api.call('GET',cases)}
                    report['checks'].append({'name':'deadline-drains-private-copy','copy_http':504})

            if not baseline:
                staging=api.root/'files'/'.case-copy-staging'
                early={'name':'Interrupted during preparation'}
                exit_proof=interrupt_and_restart(api,proof,case_route,early,email,'crash-preparation',85)
                assert (api.root/'prepare-exited').is_file()
                partial=list(staging.glob('*/*/*/Ref/archive-large.bin'))
                assert len(partial)==1 and 0<partial[0].stat().st_size<expected['size'],partial
                assert early['name'] not in {row['name'] for row in api.call('GET',cases)}
                retry=api.call('POST',case_route,early)
                assert not partial[0].exists() and retry['name']==early['name']
                report['checks'].append({'name':'interrupted-preparation-scratched-and-retried',**exit_proof})

                late={'name':'Interrupted after installation'}
                exit_proof=interrupt_and_restart(api,proof,case_route,late,email,'crash-install',86)
                plan=json.loads((api.root/'uncommitted-install.json').read_bytes())
                installed=api.root/'files'/plan['binding']['prefix']/LARGE
                assert installed.stat().st_size==expected['size']
                assert late['name'] not in {row['name'] for row in api.call('GET',cases)}
                recovery=api.call('POST','/case-provisions:recover')['results']
                assert any(item.get('id')==plan['id'] and item['status']=='rolled_back' for item in recovery),recovery
                assert not installed.exists()
                retry=api.call('POST',case_route,late)
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                                  trust_env=False,timeout=60) as client:
                    assert digest_response(client,retry['id'],LARGE)==expected
                assert not list(staging.glob('*/*/owner.json'))
                report['checks'].append({'name':'installed-before-witness-recovered-and-retried','plan_id':plan['id'],**exit_proof})
                report['restarted_api_process']=api.call('GET',proof,api_prefix=False)['identity']
            report['node_reserved_after']=api.call('GET',proof,api_prefix=False)['reserved']
            assert report['node_reserved_after']==0
        assert api.proc.poll() is not None
        report['api_exited']=True
        report['passed']=True
    finally:
        (api.root/'final-copy.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    verify(parser.parse_args().baseline)
