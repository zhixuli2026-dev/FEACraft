"""Real large retained-file restore planning concurrent with a same-case save."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx

from app.services.process_identity_native import probe
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER


BLOCK = b'retained case file\n'*2700
SIZE = 40*1024*1024
MEMBER = 'before/0'

FIXTURE = r'''
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.main import app
from app.models import Membership, Workspace
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects, manual_file_recovery as manual, sharing, s3_file_transactions
from app.services.process_identity_native import observe

root = Path(__file__).parent
member = 'before/0'

@app.get('/api/v1/test/restore-plan-birth')
async def birth(user=Depends(get_current_user)):
    return observe(os.getpid())

@app.post('/api/v1/test/restore-plan-seed/{case_id}')
async def seed(case_id:int, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case = await get_owned_case(case_id,user,db,write=True)
        identifier = str(uuid.uuid4())
        if storage._use_s3():
            source = 'objects/'+identifier
            relative = s3_file_transactions._directory(identifier)+'/'+member
            with (root/'retained.bin').open('rb') as stream:
                storage.s3().put_object(**attempt_objects.arguments(case.s3_prefix, relative),Body=stream,IfNoneMatch='*')
        else:
            source = 'preparing-'+identifier
            relative = manual.journal.PREFIX+'/'+source+'/'+member
            target = storage._fs_path(case.s3_prefix,relative)
            target.parent.mkdir(parents=True)
            shutil.copyfile(root/'retained.bin',target)
        return {'source':source,'prefix':case.s3_prefix,'workspace_id':case.workspace_id}

@app.post('/api/v1/test/restore-plan-viewer/{case_id}')
async def viewer(case_id:int, body:dict, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case = await get_owned_case(case_id,user,db,write=True)
        workspace = await db.get(Workspace,case.workspace_id)
        org = await db.scalar(select(Membership.org_id).where(Membership.user_id==user.id,Membership.role=='admin').limit(1))
        db.add(Membership(user_id=body['user_id'],org_id=org,role='viewer'))
        await sharing.set_share(db,workspace,org,'viewer',True)
        await db.commit()
        return {'org_id':org}

selected = manual.selected

def observed(case, before, after, path):
    if (root/'arm').exists() and not (root/'selection-entered').exists():
        (root/'selection-entered').touch()
        deadline = time.monotonic()+58
        while not (root/'release-selection').exists():
            if time.monotonic()>=deadline:
                raise TimeoutError('Driver did not release the real retained-file reader')
            time.sleep(.03)
    return selected(case,before,after,path)

manual.selected = observed
'''


def poll(read, check, seconds=15):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        observed = read()
        if check(observed):
            return observed
        time.sleep(.05)
    raise TimeoutError(f'Retained-file selection did not reach checkpoint: {observed}')


def call(api, method, path, body=None, *, key=None, token=None, expected=200):
    headers = {'Authorization':'Bearer '+(api.token if token is None else token)}
    if key is not None:
        headers['Idempotency-Key']=key
    with httpx.Client(timeout=150,trust_env=False) as client:
        reply = client.request(method,api.base+'/api/v1'+path,json=body,headers=headers)
    if expected is None:
        return reply.status_code
    assert reply.status_code==expected, (method,path,reply.status_code,reply.text[:600])
    return reply.json()


def content(path):
    total = 0
    digest = hashlib.sha256()
    with path.open('xb') as stream:
        while total<SIZE:
            block = BLOCK[:SIZE-total]
            stream.write(block)
            digest.update(block)
            total+=len(block)
    return {'size':total,'sha256':digest.hexdigest()}


def write_lock_available(api):
    with closing(sqlite3.connect(api.root/'test.sqlite',timeout=.15)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error):
                raise
            return False
        db.rollback()
        return True


def case_sequence(api):
    uri = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as db:
        return db.execute('SELECT next_seq FROM case_event_counters WHERE case_id=?',(api.case_id,)).fetchone()[0]


def body(plan, action, **extra):
    return {'action':action,'source':plan.get('source','pending'),'snapshot_sha256':plan['snapshot_sha256'],
            'expected_revision':plan['expected_revision'],'expected_event_sequence':plan['expected_event_sequence'],
            'note':'Actual retained file restore plan acceptance',**extra}


def download(api, target):
    digest = hashlib.sha256()
    size = 0
    with httpx.Client(timeout=110,trust_env=False) as client:
        with client.stream('GET',api.base+'/api/v1'+api.prefix+'/raw',params={'path':target},
                           headers={'Authorization':'Bearer '+api.token}) as result:
            assert result.status_code==200, result.status_code
            for block in result.iter_bytes(65536):
                digest.update(block)
                size+=len(block)
    return {'size':size,'sha256':digest.hexdigest()}


def corrupt(api, s3, prefix, isolated):
    retained = isolated['result']['retained_path']+'/'+MEMBER
    if s3 is None:
        with (api.root/'files'/prefix/retained).open('r+b') as stream:
            stream.write(b'X')
        return
    from shutil import copyfile
    wrong = api.root/'wrong.bin'
    copyfile(api.root/'retained.bin',wrong)
    with wrong.open('r+b') as stream:
        stream.write(b'X')
    with wrong.open('rb') as stream:
        s3.raw_client.put_object(Bucket=s3.bucket,Key=prefix.rstrip('/')+'/'+retained,Body=stream)


def verify(api,s3,expected,baseline,report):
    api.create_case('Actual large retained file plan')
    birth = api.call('GET','/test/restore-plan-birth')
    seed = api.call('POST',f'/test/restore-plan-seed/{api.case_id}')
    entry = api.prefix+'/file-recovery'
    plan = call(api,'GET',entry+'?source='+seed['source'])
    assert any(row['path']==MEMBER and {k:row[k] for k in expected}==expected for row in plan['files']), plan
    isolate = call(api,'POST',entry+'/actions',body(plan,'isolate_version'),key=uuid.uuid4().hex,expected=202)
    assert isolate['state']=='completed', isolate
    path = entry+f"/actions/{isolate['id']}/restore-plan?path={MEMBER}"
    seq_before = case_sequence(api)
    (api.root/'arm').touch()
    before_release = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        planned = pool.submit(call,api,'GET',path)
        poll((api.root/'selection-entered').exists,bool)
        try:
            lock_available = write_lock_available(api)
            assert lock_available is not baseline, (lock_available,baseline)
            writer = pool.submit(call,api,'PUT',api.prefix+'/file',{'path':'Ref/unrelated.txt','content':'while reading retained file'},expected=None)
            if baseline:
                try:
                    before_release = writer.result(timeout=1)
                except FutureTimeout:
                    before_release = 'waiting'
                assert before_release!=200, before_release
                assert case_sequence(api)==seq_before
            else:
                before_release = writer.result(timeout=44)
                assert before_release==200 and case_sequence(api)>seq_before, before_release
        finally:
            (api.root/'release-selection').touch()
        selected = planned.result(timeout=85)
        write_status = writer.result(timeout=45)
    assert selected['file']==expected and selected['available'], selected
    report['observed'] = {'plan':selected,'write_lock_available_while_paused':lock_available,
        'writer_http_before_release':before_release,'writer_http_final':write_status,'initial_sequence':seq_before}
    if baseline:
        return birth

    assert selected['expected_event_sequence']==case_sequence(api)>seq_before
    target = 'Ref/restored-large.bin'
    original_key = uuid.uuid4().hex
    payload = body(selected,'restore_file',isolation_id=isolate['id'],file_path=MEMBER,target_path=target)
    restored = call(api,'POST',entry+'/actions',payload,key=original_key,expected=202)
    assert restored['state']=='completed' and download(api,target)==expected, restored
    assert call(api,'POST',entry+'/actions',payload,key=original_key,expected=202)==restored

    viewer = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
                       'name':'Historical reader','password':'isolated-test-123'})
    token = viewer['token']
    viewer_id = call(api,'GET','/auth/me',token=token)['id']
    call(api,'GET',path,token=token,expected=404)
    share = call(api,'POST',f'/test/restore-plan-viewer/{api.case_id}',{'user_id':viewer_id})
    readonly = call(api,'GET',path,token=token)
    assert readonly['file']==expected and not readonly['available'] and readonly['blocked']['code']=='read_only', readonly
    (api.root/'selection-entered').unlink()
    (api.root/'release-selection').unlink()
    with ThreadPoolExecutor(max_workers=1) as pool:
        viewing = pool.submit(call,api,'GET',path,token=token,expected=None)
        poll((api.root/'selection-entered').exists,bool)
        try:
            call(api,'PUT',f"/workspaces/{seed['workspace_id']}/sharing",
                 {'org_id':share['org_id'],'role':'viewer','enabled':False})
        finally:
            (api.root/'release-selection').touch()
        assert viewing.result(timeout=85)==404
    call(api,'GET',path,token=token,expected=404)
    corrupt(api,s3,seed['prefix'],isolate)
    rejected = call(api,'GET',path,expected=409)
    assert rejected['detail']['code']=='input_stale', rejected
    assert download(api,target)==expected
    report.update(restored_sha256=expected['sha256'],same_key_receipt=True,viewer_read_only=True,
                  access_revoked_while_verifying=True,same_length_retained_corruption_rejected=True)
    return birth


def main():
    baseline = os.environ.get('VIBEFLOW_MANUAL_PLAN_BASELINE')=='1'
    remote = os.environ.get('VIBEFLOW_MANUAL_PLAN_S3')=='1'
    api = IsolatedServer('manual-restore-plan',app='restore_plan_fixture:app',access_log=True)
    expected = content(api.root/'retained.bin')
    report = {'passed':False,'backend':'SeaweedFS' if remote else 'fs','baseline':baseline,
              'api':str(api.root),'expected':expected,'model_calls':0,'mock_llm_calls':0}
    store = (ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                         volume_max=16,authenticated=True) if remote else None)
    try:
        with store if store is not None else nullcontext():
            env = {'TEMP':str(api.root),'TMP':str(api.root),'TMPDIR':str(api.root)}
            if store is not None:
                report['object_store']=str(store.root)
                env.update(store.settings())
            (api.root/'restore_plan_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE,
                                                            encoding='utf8')
            with api:
                birth = verify(api,store,expected,baseline,report)
            report['api_exit']=poll(lambda:probe(birth),lambda value:value['state']=='exited')
        if store is not None:
            assert store.proc.poll() is not None
            report['object_store_launcher_exited']=True
        report['passed']=True
    except BaseException as error:
        report.update(error=type(error).__name__,detail=str(error)[:4000]);raise
    finally:
        path = api.root/'manual-restore-plan.json'
        path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
        print(f'MANUAL_RESTORE_PLAN_RESULT {path}',flush=True)


if __name__=='__main__':
    main()
