"""Real S3 signatures, platform range reads and Chromium editor downloads."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from live_llm_job_repeat_acceptance import main
from live_llm_failure_fallback_acceptance import kill_owned_api
from live_llm_outbox_restart_acceptance import restart


NAME='J28-download.bin'
PAYLOAD=bytes(range(256))
CHANGED=b'new-version-'+PAYLOAD+PAYLOAD

FIXTURE = '''
import os
from pathlib import Path
from app import storage
from app.services.s3_live_store import Client,split
root=Path(__file__).parent
(root/'download-api-pid').write_text(str(os.getpid()))
select_entry=Client._selected
def select_then_replace(self,args,operation):
    selected=select_entry(self,args,operation)
    change=root/'replace-during-signing'
    if change.exists() and selected and operation=='GetObject' and args['Key'].endswith('/J28-download.bin'):
        change.unlink()
        prefix,path=split(args['Key'])
        storage.put_bytes(prefix,path,b'new-version-'+bytes(range(256))*2)
    return selected
Client._selected=select_then_replace
'''

BROWSER = '''
import hashlib,json,sys
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
d=json.load(sys.stdin)
with sync_playwright() as play:
    browser=play.chromium.launch()
    try:
        ctx=browser.new_context(viewport={'width':1600,'height':1000},locale='zh-CN')
        ctx.add_init_script("if(location.origin==="+json.dumps(d['base'])+")localStorage.setItem('vf_token',"+json.dumps(d['token'])+");")
        page=ctx.new_page()
        observed=[]
        def request(req):
            url=urlsplit(req.url)
            signed='X-Amz-Signature=' in url.query
            raw=url.path.endswith('/raw') and d['name'] in url.query
            if signed or raw:
                observed.append({'signed':signed,'raw':raw,'bearer_present':'authorization' in req.all_headers()})
        page.on('request',request)
        page.goto(d['base'],wait_until='domcontentloaded')
        page.locator('[data-case-name="'+d['case_name']+'"]').click(timeout=30000)
        page.locator('[data-path="'+d['name']+'"]').click(timeout=30000)
        with page.expect_download(timeout=30000) as started:
            page.get_by_role('button',name='下载',exact=True).click(timeout=15000)
        item=started.value
        assert item.suggested_filename==d['name']
        digest=hashlib.sha256(Path(item.path()).read_bytes()).hexdigest()
        assert digest==d['digest']
        assert any(o['signed']==d['s3'] and o['bearer_present']!=d['s3'] for o in observed)
        assert all(not o['bearer_present'] for o in observed if o['signed'])
        (Path(d['root'])/'browser-download.json').write_text(json.dumps(dict(filename=item.suggested_filename,
            sha256=digest,requests=observed),indent=2))
    finally:
        browser.close()
'''


def scenario(api, *, store=None, **_):
    versioned=bool(store and api.database_url)
    if versioned:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    case_name='Download acceptance '+uuid.uuid4().hex[:8]
    api.create_case(case_name)
    headers={'Authorization':'Bearer '+api.token}
    url=api.prefix+'/raw'
    params={'path':NAME}
    signed_checks=0
    with httpx.Client(base_url=api.base+'/api/v1',trust_env=False,timeout=90) as client:
        def upload(data):
            r=client.post(api.prefix+'/upload',headers=headers,params=params,files={'file':(NAME,data,'application/octet-stream')})
            assert r.status_code==200,r.status_code
        def info():
            r=client.get(api.prefix+'/file',params=params,headers=headers)
            assert r.status_code==200,r.status_code
            return r.json()
        def raw(method='GET', **extra):
            return client.request(method,url,params=params,headers={**headers,**extra})
        upload(PAYLOAD)
        meta=info()
        assert meta['kind']=='binary' and meta['size']==len(PAYLOAD)
        signed=meta['download_url']
        version=raw('HEAD').headers['x-vibeflow-file-version']
        for method in ('GET','HEAD'):
            full=raw(method)
            assert full.status_code==200 and full.content==(PAYLOAD if method=='GET' else b'')
            assert int(full.headers['content-length'])==256 and full.headers['accept-ranges']=='bytes'
            for value,start,end in [('0-99',0,99),('200-',200,255),('-16',240,255)]:
                r=raw(method,Range='bytes='+value)
                assert r.status_code==206 and r.headers['content-range']==f'bytes {start}-{end}/256'
                assert r.content==(PAYLOAD[start:end+1] if method=='GET' else b'') and int(r.headers['content-length'])==end-start+1
            for bad in ('bytes=256-','bytes=oops'):
                r=raw(method,Range=bad)
                assert r.status_code==416 and r.headers['content-range']=='bytes */256'
        multi=raw(Range='bytes=1-3,200-202')
        assert multi.status_code==206 and 'multipart/byteranges' in multi.headers['content-type']
        assert PAYLOAD[1:4] in multi.content and PAYLOAD[200:203] in multi.content
        other=client.post('/auth/register',json={'email':uuid.uuid4().hex+'@example.com','name':'Second user','password':'isolated-pass-123'})
        assert other.status_code==200,other.status_code
        for path in (url,api.prefix+'/file'):
            assert client.get(path,params=params).status_code in {401,403}
            assert client.get(path,params=params,headers={'Authorization':'Bearer '+other.json()['token']}).status_code in {403,404}
        if store:
            target=urlsplit(signed)
            assert target.hostname=='127.0.0.1' and 'X-Amz-Signature' in parse_qs(target.query)
            assert client.get(signed.partition('?')[0]).status_code==403
            bucket,key=unquote(target.path).lstrip('/').split('/',1)
            assert bucket==store.bucket
            params_head={'Bucket':bucket,'Key':key}
            if versioned:
                selected_version,=parse_qs(target.query)['versionId']
                params_head['VersionId']=selected_version
            head_url=store.raw_client.generate_presigned_url('head_object',Params=params_head,ExpiresIn=60)
            physical=client.head(head_url)
            assert physical.status_code==200 and not physical.content and physical.headers['content-length']=='256'
            for auth in ({},{'Authorization':'Bearer unrelated-client-value'}):
                r=client.get(signed,headers=auth)
                assert r.status_code==200 and r.content==PAYLOAD,r.status_code
                signed_checks+=1
            for value,start,end in [('0-6',0,6),('200-',200,255),('-7',249,255)]:
                r=client.get(signed,headers={'Range':'bytes='+value})
                assert r.status_code==206 and r.content==PAYLOAD[start:end+1]
                signed_checks+=1
            unsatisfied=client.get(signed,headers={'Range':'bytes=256-300'})
            assert unsatisfied.status_code==416,unsatisfied.status_code
            if versioned:
                # Older namespaces have no logical catalog. Pin their native version ID.
                legacy='legacy-download/example.bin'
                store.raw_client.put_object(Bucket=bucket,Key=legacy,Body=PAYLOAD)
                size,prior=store.client.download_metadata(bucket,legacy,60)
                store.raw_client.put_object(Bucket=bucket,Key=legacy,Body=CHANGED)
                assert size==len(PAYLOAD) and client.get(prior).content==PAYLOAD
                current_size,current=store.client.download_metadata(bucket,legacy,60)
                assert current_size==len(CHANGED) and client.get(current).content==CHANGED
            # In the isolated bucket, replace the real logical file after selection.
            (api.root/'replace-during-signing').touch()
            selected=info()
            assert not (api.root/'replace-during-signing').exists()
            assert selected['size']==256 and client.get(selected['download_url']).content==PAYLOAD
            assert info()['size']==len(CHANGED)
        else:
            upload(CHANGED)
        assert raw(**{'X-Vibeflow-File-Version':version}).status_code==409
        kill_owned_api(api,int((api.root/'download-api-pid').read_text()))
        restart(api)
        assert raw().content==CHANGED
        if store:
            assert client.get(signed).content==PAYLOAD
            assert client.get(info()['download_url']).content==CHANGED
        data=dict(base=api.base,root=str(api.root),token=api.token,name=NAME,case_name=case_name,
                  digest=hashlib.sha256(CHANGED).hexdigest(),s3=bool(store))
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],'-c',BROWSER],input=json.dumps(data),
                       text=True,check=True,timeout=110)
        missing=client.get(api.prefix+'/file',params={'path':'not-present.bin'},headers=headers)
        assert missing.status_code==404
    result={'backend':'s3' if store else 'fs','db':'postgres' if api.database_url else 'sqlite',
            'raw_GET_HEAD_range':True,'direct_signed_checks':signed_checks,'unauthenticated_and_other_user_refused':True,
            'stale_read_http':409,'api_restarted':True,'original_url_read_old_version_after_restart':bool(store),
            'unsigned_object_http':403 if store else None,'signed_direct_HEAD':bool(store),
            'signed_direct_unsatisfiable_http':416 if store else None,
            'bucket_versioning':versioned,'legacy_versioned_read_verified':versioned,
            'missing':404,'browser':json.loads((api.root/'browser-download.json').read_text())}
    (api.root/'file-download.json').write_text(json.dumps(result,indent=2))
    return result


if __name__=='__main__':
    bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    from s3testsupport import ObjectStore
    from testsupport import SERVER
    def private_store():
        return ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=6,authenticated=True)
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE+mount,store_volume_max=6,store_factory=private_store))
