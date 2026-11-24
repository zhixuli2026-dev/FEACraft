"""Observe real authenticated S3 derivation, concurrent SQL and journal rollback."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

from case_copy_source_lock_smoke import RUNTIME, digest_response, read_observation, writer_available
from run_derive_source_lock_smoke import BASE, CONTROL, EDIT, LARGE, SIZE, seed
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER

FIXTURE = r'''
import os
import time
import json
from contextvars import ContextVar
from pathlib import Path
from fastapi import Depends
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import run_derive, s3_file_transactions, s3_live_store, node_storage_budget
from app.services import process_identity_native
from app.services.request_deadline import current

root = Path(__file__).parent
request_state = ContextVar('derive_transfer_observation', default=None)
source_copy = ContextVar('derive_transfer_source_copy', default=False)
original_snapshot = s3_file_transactions.snapshot
original_inventory = run_derive.remote_inventory
original_get_object = s3_live_store.Client.get_object

def pause(name, **details):
    budget = current()
    if budget is not None:
        details['remaining'] = budget.remaining()
    path = root/(name+'.json')
    scratch = path.with_suffix('.new')
    scratch.write_text(json.dumps(details), encoding='utf8')
    scratch.replace(path)
    deadline = time.monotonic()+30
    while not (root/('release-'+name)).exists():
        if time.monotonic()>=deadline:
            raise TimeoutError('The isolated remote observation was not released')
        time.sleep(.02)

class ObservedBody:
    def __init__(self, body):
        self.body = body
        self.first = True

    def __getattr__(self, name):
        return getattr(self.body,name)

    def read(self, size):
        block = self.body.read(size)
        if block and self.first:
            self.first = False
            pause('physical-copy', bytes=len(block), node_claimed=node_storage_budget.observe()['reserved_bytes'])
        return block

def observe_get_object(self, **options):
    result = original_get_object(self, **options)
    if source_copy.get() and options['Key'].endswith('/C-run/derive-source/0/source-field.bin'):
        result['Body'] = ObservedBody(result['Body'])
    return result

def observe_snapshot(prefix, path, **options):
    state = request_state.get()
    active = state is not None and state['mode']=='copy' and options.get('output') is not None
    marker = source_copy.set(active)
    try:
        return original_snapshot(prefix,path,**options)
    finally:
        source_copy.reset(marker)

def observe_inventory(prefix, base):
    state = request_state.get()
    if state is not None:
        state['scans'] += 1
        if state['scans']==2 and state['mode'] in {'source-change','timeout','crash'}:
            pause(state['mode'])
    return original_inventory(prefix,base)

s3_file_transactions.snapshot = observe_snapshot
s3_live_store.Client.get_object = observe_get_object
run_derive.remote_inventory = observe_inventory

@app.get('/fixture/derive-transfer/{case_id}')
async def binding(case_id:int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db,write=False)
    return {'prefix':case.s3_prefix,'pid':process_identity_native.observe(os.getpid()),
            'node_claimed':node_storage_budget.observe()['reserved_bytes']}

@app.post('/fixture/derive-transfer:exit')
async def exit_after_writes(user=Depends(get_current_user)):
    assert (root/'crash.json').is_file() and not (root/'release-crash').exists()
    os._exit(86)

class ObserveRequest:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        observation = dict(scope.get('headers',())).get(b'x-fixture-derive-s3')
        if not scope.get('path','').endswith('/runs:derive') or observation is None:
            return await self.app(scope,receive,send)
        selected = observation.decode('ascii')
        marker = request_state.set({'mode':selected,'scans':0})
        settings = get_settings()
        old_limit = settings.run_derive_timeout_seconds
        if selected=='timeout':
            settings.run_derive_timeout_seconds = 20
        try:
            return await self.app(scope,receive,send)
        finally:
            request_state.reset(marker)
            settings.run_derive_timeout_seconds = old_limit

app = ObserveRequest(app)
'''


def journal_rows(api, case_id):
    dbpath = (api.root/'test.sqlite').resolve()
    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as db:
        rows = db.execute('SELECT state,journal_id FROM case_storage_operations WHERE case_id=? ORDER BY created_at,id',
                          (case_id,)).fetchall()
        unsettled = db.execute("SELECT count(*) FROM storage_write_reservations WHERE case_id=? AND state='active'",
                               (case_id,)).fetchone()[0]
    return rows, unsettled


def verify(baseline):
    api = IsolatedServer('run-derive-s3-final', app='derive_transfer_fixture:app')
    receipt = api.root/'derive-s3-final.json'
    report = {'passed':False,'baseline':baseline,'api_port':api.port,'actual_model_calls':0,'mock_llm_calls':0,'checks':[]}
    print('DERIVE_S3_FINAL_EVIDENCE '+str(api.root),flush=True)

    def save():
        receipt.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')

    try:
        with ObjectStore(executable=RUNTIME,volume_max=32,authenticated=True) as store:
            report['store_receipt'] = str(store.root/'store.json')
            module = api.root/'derive_transfer_fixture.py'
            module.write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,encoding='utf8')
            try:
                with api:
                    exercise(api,store,report,baseline,save)
            finally:
                module.unlink(missing_ok=True)
        assert store.proc.poll()==0 and api.proc.poll() is not None
        report['store_exit_code']=store.proc.returncode
        report['api_exit_code']=api.proc.returncode
        report['passed']=True
    finally:
        save()


def exercise(api,store,report,baseline,save):
    case = api.create_case('S3 derived directory')
    email = api.call('GET','/auth/me')['email']
    workspace = api.call('GET','/workspaces')[0]
    other = api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'Independent storage writer'})
    fixture = f'/fixture/derive-transfer/{case["id"]}'
    bound = api.call('GET',fixture,api_prefix=False)
    prefix = bound['prefix'].rstrip('/')+'/'
    report['api_process']=bound['pid']
    api.put(BASE+'/system/controlDict',CONTROL)
    api.put(EDIT,'source value A')
    report['source'] = seed(api,store,bound['prefix'])
    endpoint = api.prefix+'/runs:derive'
    bucket = store.bucket

    def visible(target):
        names=store.client.list_objects_v2(Bucket=bucket,Prefix=prefix+target+'/').get('Contents',[])
        return [entry['Key'][len(prefix):] for entry in names]

    def resolution(case_id,target):
        rows, pending = journal_rows(api,case_id)
        state, journal = next(row for row in reversed(rows) if row[1] is not None)
        assert state=='settled' and pending==0,(rows,pending)
        key=prefix+'.vibeflow/file-transactions/objects/'+journal+'/resolution.json'
        result=store.client.get_object(Bucket=bucket,Key=key)
        try:
            resolved=json.load(result['Body'])
        finally:
            result['Body'].close()
        assert resolved['decision']=='rolled_back' and visible(target)==[],(resolved,visible(target))
        return resolved

    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      trust_env=False,timeout=170) as client:
        def issue(worker,mode,target,base=BASE):
            return worker.submit(client.post,endpoint,json={'base':base,'name':target},headers={'X-Fixture-Derive-S3':mode})

        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=issue(worker,'copy','copy-concurrency')
            try:
                block=read_observation(api,'physical-copy.json')
                assert block['bytes'] and block['node_claimed']>=SIZE and not pending.done(),block
                report['db_writer_available']=writer_available(api)
                try:
                    unrelated=client.put(f'/cases/{other["id"]}/file',json={'path':'other.txt','content':'independent'},
                                         timeout=4 if baseline else 20)
                except httpx.ReadTimeout:
                    if not baseline:
                        raise
                    unrelated=None
                same=None if baseline else client.put(api.prefix+'/file',json={'path':'same.txt','content':'too early'},timeout=20)
            finally:
                (api.root/'release-physical-copy').touch()
            copied=pending.result(timeout=170)
        unrelated_status = 'observation_timeout' if unrelated is None else unrelated.status_code
        report['checks'].append({'name':'concurrent_physical_copy','unrelated_case_save':unrelated_status,
            'same_case_save':None if same is None else same.status_code,'derive':copied.status_code,'first_chunk':block['bytes']})
        save()
        assert report['db_writer_available'] is (not baseline) and copied.status_code==200, copied.text
        if baseline:
            assert unrelated_status in {409,'observation_timeout'},unrelated_status
            return
        assert unrelated_status==200, unrelated.text
        assert same.status_code==409 and same.json()['detail']['code']=='file_sync_busy',same.text
        assert digest_response(client,case['id'],'C-run/copy-concurrency/0/source-field.bin')==report['source']

        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=issue(worker,'source-change','conflict-copy')
            target='C-run/conflict-copy'
            try:
                read_observation(api,'source-change.json')
                assert len(visible(target))==3 and writer_available(api)
                expected=store.client.head_object(Bucket=bucket,Key=prefix+EDIT)['ETag']
                store.client.put_object(Bucket=bucket,Key=prefix+EDIT,Body=b'source value B',IfMatch=expected)
            finally:
                (api.root/'release-source-change').touch()
            conflict=pending.result(timeout=170)
        assert conflict.status_code==409,conflict.text
        report['checks'].append({'name':'changed_source_before_commit_rolled_back','http':conflict.status_code,
            'resolution':resolution(case['id'],target)})
        assert client.get(api.prefix+'/raw',params={'path':EDIT}).text=='source value B'
        retry=client.post(endpoint,json={'base':BASE,'name':'conflict-copy'})
        assert retry.status_code==200 and client.get(api.prefix+'/raw',params={'path':target+'/0/initial-note.txt'}).text=='source value B'
        assert digest_response(client,case['id'],target+'/0/source-field.bin')==report['source']
        report['checks'].append({'name':'same_name_retry_after_rollback','http':retry.status_code})
        save()

        base='C-run/small-timeout-source'
        api.put(base+'/system/controlDict',CONTROL)
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=issue(worker,'timeout','timeout-copy',base=base)
            target='C-run/timeout-copy'
            try:
                observed=read_observation(api,'timeout.json')
                assert visible(target)==[target+'/system/controlDict'] and observed['remaining']>0,observed
                time.sleep(observed['remaining']+.15)
                assert not pending.done()
            finally:
                (api.root/'release-timeout').touch()
            expired=pending.result(timeout=170)
        assert expired.status_code==504 and expired.json()['detail']['code']=='run_derive_timeout',expired.text
        report['checks'].append({'name':'expired_request_drains_and_rolls_back','http':expired.status_code,
            'resolution':resolution(case['id'],target)})
        assert client.post(endpoint,json={'base':base,'name':'timeout-copy'}).status_code==200

        with ThreadPoolExecutor(max_workers=1) as worker:
            pending=issue(worker,'crash','crash-copy',base=base)
            target='C-run/crash-copy'
            try:
                read_observation(api,'crash.json')
                rows, unsettled = journal_rows(api,case['id'])
                assert rows[-1][0]=='active' and rows[-1][1] is not None and unsettled==0,(rows,unsettled)
                assert visible(target)==[target+'/system/controlDict']
                old_api=api.proc
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},
                                  trust_env=False,timeout=15) as auxiliary:
                    try:
                        auxiliary.post('/fixture/derive-transfer:exit')
                    except (httpx.RemoteProtocolError, httpx.ReadError):
                        pass
                    else:
                        raise AssertionError('The isolated API did not exit')
                assert old_api.wait(timeout=15)==86
                api.__exit__(None,None,None)
                assert old_api.poll() is not None
                try:
                    pending.result(timeout=30)
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('The interrupted request produced an HTTP reply')
            finally:
                (api.root/'release-crash').touch()

    from app.services import process_identity_native
    for _ in range(100):
        vanished=process_identity_native.probe(report['api_process'])
        if vanished['state']=='exited':
            break
        time.sleep(.05)
    assert vanished['state']=='exited',vanished
    report['crashed_api_exit_code']=old_api.returncode
    api.__enter__()
    api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
    report['restarted_api_process']=api.call('GET',fixture,api_prefix=False)['pid']
    recovered=api.call('POST',api.prefix+'/file-transactions:recover',{})['result']
    assert recovered==resolution(case['id'],target), recovered
    assert api.call('POST',endpoint,{'base':base,'name':'crash-copy'})['files']==1
    report['checks'].append({'name':'actual_api_exit_then_cold_recovery','resolution':recovered,'retry':200})
    report['node_reserved_after']=api.call('GET',fixture,api_prefix=False)['node_claimed']
    assert report['node_reserved_after']==0
    save()


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',action='store_true')
    verify(parser.parse_args().baseline)
