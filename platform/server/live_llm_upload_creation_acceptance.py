"""Keyed uploads through the real API and a production Chromium bundle."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import uuid

import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_outbox_restart_acceptance import restart


FIXTURE = '''
import os
from botocore.client import BaseClient
from app.services import uploads
from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.models import OwnerStorageReservation
from app.services import owner_storage_budget
from app.services.storage_admission import reserved_bytes,lock_owner

@app.get('/api/v1/testing/creation-credit')
async def available_credit(user=Depends(get_current_user), db=Depends(get_db)):
    if storage._use_s3():
        await owner_storage_budget.recover_owner(user.id,wait=True)
    await lock_owner(db,user.id,wait=True)
    count=await reserved_bytes(db,user.id)
    rows=await db.scalars(select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id))
    proofs=[(json.loads(r.receipt_json).get('upload_chunk_receipt') or {}).get('schema') for r in rows]
    await db.commit()
    return {'reserved':count,'matched_creations':proofs.count('vibeflow.upload-create-proof.v1')}

def cut(stage):
    flag = root / 'creation-cut'
    if flag.exists() and flag.read_text() == stage:
        os._exit(86)

write_meta = uploads._write_meta
def local_write(directory, metadata):
    cut('before')
    write_meta(directory, metadata)
    cut('after')
uploads._write_meta = local_write

send = BaseClient._make_api_call
def send_object(client, operation, args):
    initial = (operation == 'PutObject' and '/.vibeflow/uploads/' in args.get('Key','')
               and args['Key'].endswith('/meta.json') and args.get('IfNoneMatch') == '*')
    if initial: cut('before')
    result = send(client, operation, args)
    if initial: cut('after')
    return result
BaseClient._make_api_call = send_object
'''


BROWSER = r'''
import hashlib, json, sys
from pathlib import Path
from playwright.sync_api import expect, sync_playwright
d=json.load(sys.stdin)
with sync_playwright() as play:
    browser=play.chromium.launch()
    try:
        ctx=browser.new_context(viewport={'width':1600,'height':1000},locale='zh-CN')
        ctx.add_init_script("if(location.origin==="+json.dumps(d['base'])+")localStorage.setItem('vf_token',"+json.dumps(d['token'])+");")
        page=ctx.new_page()
        page.set_default_timeout(90000)
        url=d['base']+'/api/v1'+d['prefix']+'/uploads'
        lost=[]
        keys=[]
        offsets=[]
        failures=[]
        duplicate_keys=[]
        page.on('console',lambda message: duplicate_keys.append(message.text)
                if 'Encountered two children with the same key' in message.text else None)
        def failed_response(response):
            if response.status>=400 and response.url.startswith(url):
                failures.append({'method':response.request.method,'status':response.status,
                                 'endpoint':response.url[len(url):].split('?')[0]})
        page.on('response',failed_response)
        def observe(req):
            if req.url==url and req.method=='POST':
                keys.append(req.all_headers().get('idempotency-key'))
            if req.method=='PUT' and req.url.startswith(url+'/') and '?offset=' in req.url:
                offsets.append(int(req.url.split('?offset=')[1]))
        page.on('request',observe)
        def open_case():
            page.goto(d['base'], wait_until='domcontentloaded')
            page.locator('[data-case-name="'+d['name']+'"]').click()
            expect(page.get_by_test_id('case-file-actions')).to_have_count(1)
            expect(page.get_by_test_id('tree-upload-file')).to_have_count(1)
        def lose(route):
            response=route.fetch(timeout=120000)
            assert response.ok,(response.status,response.text()[:100])
            lost.append(response.json())
            route.abort()
        submit=page.get_by_test_id('file-operation-submit')
        def choose(selector,filename,contents):
            page.get_by_test_id(selector).set_input_files({'name':filename,'mimeType':'application/octet-stream','buffer':contents})
        def error():
            page.get_by_test_id('file-operation-error').filter(has_text='网络请求中断').wait_for()
        def reselect(contents):
            page.wait_for_function("!document.querySelector('[data-testid=file-operation-cancel]')?.disabled")
            choose('file-resume-source','renamed.bin',contents)
            page.get_by_test_id('file-operation-notice').filter(has_text='校验通过').wait_for()
        data=(bytes(range(256))*69633)[:17*1024*1024+13]
        open_case()
        choose('tree-upload-file','browser-recovery.bin',data)
        page.get_by_test_id('file-operation-inspect').click()
        page.route(url,lose,times=1)
        submit.click()
        error()
        assert len(keys)==1 and keys[0] and len(lost)==1 and lost[0]['offset']==0
        original=lost[0]['id']
        open_case()
        reselect(data)
        page.route(url+'/'+original+'?offset=0',lose,times=1)
        submit.click()
        error()
        assert len(lost)==2 and lost[1]['id']==original and lost[1]['offset']==4194304
        open_case()
        expect(page.get_by_role('progressbar',name='文件上传进度')).to_have_attribute('value','4194304',timeout=60000)
        reselect(data)
        submit.click()
        page.get_by_test_id('file-operation-notice').filter(has_text='已上传 Ref/browser-recovery.bin').wait_for()
        assert len(keys)==1 and offsets==[0,4194304,8388608,12582912,16777216],(len(keys),offsets)
        assert not duplicate_keys, duplicate_keys
        report={'upload_id':original,'create_requests':len(keys),'offsets':offsets,'size':len(data),
                'sha256':hashlib.sha256(data).hexdigest(),'browser':browser.version,'single_upload_control':True}
        (Path(d['root'])/'browser-upload.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    except Exception:
        detail={'offsets':offsets,'failures':failures,'duplicate_keys':duplicate_keys,
                'error':page.get_by_test_id('file-operation-error').all_text_contents()}
        (Path(d['root'])/'browser-failure.json').write_text(json.dumps(detail,ensure_ascii=False),encoding='utf8')
        raise
    finally:
        browser.close()
'''


def scenario(api, *, store=None, **_):
    name='Keyed upload '+uuid.uuid4().hex[:8]
    api.create_case(name)
    base=api.prefix+'/uploads'
    intent=api.prefix+'/upload-intent'
    key={'Idempotency-Key':uuid.uuid4().hex}
    body={'path':'Ref/replay.bin','size':200,'expected_sha256':None}
    def active(path):
        return [r for r in api.call('GET',base) if r['path']==path]
    def create():
        return api.call('POST',base,body,extra_headers=key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:create(),range(2)))
    first=results[0]
    assert results[1]==first and sum(r['remaining_bytes'] for r in active(body['path']))==200
    assert api.call('GET',intent,extra_headers=key)['upload']==first
    api.call('POST',base,{**body,'size':201},expect=409,extra_headers=key)
    api.call('POST',base,{k:v for k,v in body.items() if k!='expected_sha256'},expect=409,extra_headers=key)
    api.put(body['path'],'new target')
    assert create()==first
    missing={'Idempotency-Key':'missing-'+uuid.uuid4().hex}
    assert api.call('GET',intent,extra_headers=missing)=={'upload':None}
    assert api.call('DELETE',intent,extra_headers=missing)['cancelled']
    api.call('POST',base,body,expect=409,extra_headers=missing)
    assert api.call('DELETE',intent,extra_headers=key)['cancelled']
    assert not active(body['path'])
    api.call('POST',base,body,expect=409,extra_headers=key)
    assert api.call('GET',api.prefix+'/raw?path='+body['path'],raw=True)==b'new target'
    email=uuid.uuid4().hex+'@example.com'
    other=api.call('POST','/auth/register',{'email':email,'name':'Other','password':'isolated-pass-123'})
    foreign={**key,'Authorization':'Bearer '+other['token']}
    for method,path in [('GET',intent),('DELETE',intent),('POST',base)]:
        api.call(method,path,body if method=='POST' else None,expect=404,extra_headers=foreign)
    org=api.call('GET','/org/mine')['org']['id']
    workspace=api.call('GET','/workspaces')[0]['id']
    api.call('POST',f'/org/{org}/members',{'email':email,'role':'member'},expect=201)
    api.call('POST',f'/org/{org}/invitation:accept',extra_headers=foreign)
    share='/workspaces/'+str(workspace)+'/sharing'
    api.call('PUT',share,{'org_id':org,'role':'member','enabled':True})
    shared_body={'path':'Ref/shared.bin','size':222}
    peer=api.call('POST',base,shared_body,extra_headers=foreign)
    assert peer['id']!=first['id'] and api.call('GET',intent,extra_headers=foreign)['upload']==peer
    assert api.call('GET',intent,extra_headers=key)['cancelled']
    assert api.call('GET','/testing/creation-credit')['reserved']==222
    api.call('PUT',share,{'org_id':org,'role':'viewer','enabled':True})
    api.call('DELETE',intent,expect=404,extra_headers=foreign)
    assert active(shared_body['path'])[0]['id']==peer['id']
    api.call('DELETE',base+'/'+peer['id'])
    api.call('PUT',share,{'org_id':org,'role':'member','enabled':False})

    outcomes=[]
    for stage,action in [('before','resume'),('after','resume'),('before','cancel')]:
        path=f'Ref/{stage}-{action}.bin'
        declaration={'path':path,'size':100,'overwrite':False}
        header={'Idempotency-Key':uuid.uuid4().hex}
        flag=api.root/'creation-cut'
        flag.write_text(stage)
        try:
            api.call('POST',base,declaration,extra_headers=header)
        except (http.client.RemoteDisconnected,ConnectionResetError,urllib.error.URLError):
            pass
        assert api.proc.wait(timeout=12)==86
        flag.unlink()
        restart(api)
        row,=active(path)
        assert row['remaining_bytes']==100
        if action=='resume':
            result=api.call('POST',base,declaration,extra_headers=header)
            assert result['id']==row['id'] and result['offset']==0
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                              trust_env=False,timeout=60) as client:
                endpoint=base+'/'+row['id']
                assert client.put(endpoint+'?offset=1',content=b'a').status_code==409
                client.put(endpoint+'?offset=0',content=b'Z'*100).raise_for_status()
                done=api.call('POST',endpoint+':complete')
                assert done['sha256']==hashlib.sha256(b'Z'*100).hexdigest()
                assert api.call('POST',base,declaration,extra_headers=header)['status']=='completed'
                assert client.get(api.prefix+'/raw',params={'path':path}).content==b'Z'*100
        else:
            assert api.call('DELETE',intent,extra_headers=header)['cancelled']
            api.call('POST',base,declaration,expect=409,extra_headers=header)
        assert not active(path)
        if store:
            from botocore.exceptions import ClientError
            suffix='/'+row['id']+'/meta.json'
            keys=[r['Key'] for page in store.raw_client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
                  for r in page.get('Contents',[]) if r['Key'].endswith(suffix)]
            physical,=keys
            try:
                store.raw_client.put_object(Bucket=store.bucket,Key=physical,IfNoneMatch='*',Body=b'late')
            except ClientError as error:
                assert error.response['ResponseMetadata']['HTTPStatusCode']==412
            else:
                raise AssertionError('Delayed creation recreated a terminal upload')
        outcomes.append({'stage':stage,'action':action,'reserved_after_restart':100,'active_after_settlement':0})

    payload={'base':api.base,'root':str(api.root),'prefix':api.prefix,'name':name,'token':api.token}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],'-c',BROWSER],input=json.dumps(payload),
                   text=True,check=True,timeout=450)
    browser=json.loads((api.root/'browser-upload.json').read_text())
    server_log=(api.root/'api.log').read_text(encoding='utf8', errors='replace')
    assert 'WebSocket /ws/events' in server_log and api.token not in server_log
    headers={'Authorization':'Bearer '+api.token}
    with httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=120,trust_env=False) as client:
        path=api.prefix+'/raw?path=Ref/browser-recovery.bin'
        data=client.get(path)
        assert data.status_code==200 and hashlib.sha256(data.content).hexdigest()==browser['sha256']
        head=client.head(path)
        assert head.status_code==200 and int(head.headers['content-length'])==browser['size'] and not head.content
        multi=client.get(path,headers={'Range':'bytes=1-7,1000-1011'})
        assert multi.status_code==206 and multi.headers['content-type'].startswith('multipart/byteranges')
        assert data.content[1:8] in multi.content and data.content[1000:1012] in multi.content
        assert client.get(path,headers={'Range':'bytes=0-2,1-3'}).status_code==416
    assert not active('Ref/browser-recovery.bin')
    credit=api.call('GET','/testing/creation-credit')
    assert credit['reserved']==0
    if store:assert credit['matched_creations']>=3,credit
    report={'passed':True,'backend':'s3' if store else 'fs','database':'postgres' if api.database_url else 'sqlite',
            'lost_create_and_chunk_responses':True,'concurrent_request_single_reservation':True,'withdraw_before_create':True,
            'different_request_conflict':True,'foreign_access_denied':True,'api_deaths':outcomes,'browser':browser,
            'published_file_verified':True,'multipart_ranges_verified':True,'owner_credit':credit,
            'shared_actor_keys_distinct':True,'viewer_cancel_refused':True}
    (api.root/'upload-creation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE+mount,store_volume_max=8))
