"""Real sandbox delta, API crash during bulk journal, and authorized DeepSeek readback."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time
import uuid
import httpx

from live_llm_acceptance import source, FIXTURE
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from testsupport import SERVER

ROOT=SERVER.parents[1]
FAULT=r'''
import os
from app.services import file_transactions
_actual_apply=file_transactions._apply
def exit_during_bulk(*args,**kwargs):
 result=_actual_apply(*args,**kwargs)
 flag=Path(__file__).parent/'exit-bulk-restore'
 if flag.exists() and args[2]==1 and args[4]=='after':
  flag.unlink();os._exit(83)
 return result
file_transactions._apply=exit_during_bulk
'''


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--s3',action='store_true')
    args=parser.parse_args()
    reviewed=json.loads(args.review.read_text('utf8'))
    for name,digest in reviewed['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    configuration=await source(args.source_profile_id)
    evidence={'passed':False,'mock_llm_calls':0,'checks':[],'storage':'s3' if args.s3 else 'fs','review':str(args.review)}
    actual_root=None
    async def before(api):
        nonlocal actual_root
        actual_root=api.root
        print('SANDBOX_CHANGES_ROOT '+str(api.root),flush=True)
        for path,content in [('conflict.txt','ORIGINAL'),('delete.txt','DELETE'),('stable.txt','STABLE')]:
            await asyncio.to_thread(api.call,'PUT',api.prefix+'/file',body={'path':'Ref/'+path,'content':content})
    command=("from pathlib import Path; import secrets; p=Path('Ref'); "
             "[(p/('bulk-%03d.txt'%i)).write_text(secrets.token_hex(16)) for i in range(300)]; "
             "(p/'conflict.txt').write_text('SANDBOX'); (p/'delete.txt').unlink()")
    shell='python3 -c '+shlex.quote(command)+'; '
    async def after(api,sid,rid,proof):
        async def call(method,path,**kwargs):
            if args.s3 and method=='POST' and path.endswith('/restore'):
                def bulk():
                    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                                      trust_env=False,timeout=300) as client:
                        response=client.post(path,json=kwargs['body'])
                        assert response.status_code==kwargs.get('expect',200),(response.status_code,response.text[:600])
                        return response.json()
                return await asyncio.to_thread(bulk)
            return await asyncio.to_thread(api.call,method,path,**kwargs)
        def save():
            (api.root/'changes-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n','utf8')
        def record(name):evidence['checks'].append(name);save()
        endpoint=f'/terminal/sessions/{sid}/recoveries/{rid}'
        detail=await call('GET',endpoint)
        assert 'Ref/delete.txt' in detail['baseline']['files'] and 'Ref/delete.txt' not in detail['manifest']['files']
        # Locate this operation's isolated retained snapshot by its manifest hash.
        roots=[p.parent for p in (api.root/'files/.terminal-recovery').glob('*/manifest.json')
               if hashlib.sha256(p.read_bytes()).hexdigest()==detail['receipt']['manifest_sha256']]
        assert len(roots)==1
        baseline=roots[0]/'initial.json';original=baseline.read_bytes()
        try:
            baseline.write_bytes(original+b' ')
            await call('GET',endpoint,expect=409)
        finally:baseline.write_bytes(original)
        record('initial manifest bound before terminal input, snapshot tampering refused')
        inventory=await call('GET',endpoint+'/changes')
        names=[item['path'] for item in inventory['files']]
        assert len(names)==303 and 'Ref/stable.txt' not in names
        await call('PUT',api.prefix+'/file',body={'path':'Ref/conflict.txt','content':'CURRENT'})
        selected={'paths':names,'semantics':'sandbox_file_changes'}
        plan=await call('POST',endpoint+'/restore-plan',body=selected)
        assert [f['path'] for f in plan['files'] if f['action']=='conflict']==['Ref/conflict.txt']
        rejected={**selected,'plan_sha256':plan['plan_sha256'],'request_id':str(uuid.uuid4()),'confirm':True}
        await call('POST',endpoint+'/restore',body=rejected,expect=409)
        await call('GET',api.prefix+'/raw?path=Ref/bulk-000.txt',expect=404)
        assert await call('GET',api.prefix+'/raw?path=Ref/delete.txt',raw=True)==b'DELETE'
        record('three-way conflict refuses whole batch before any mutation')
        names.remove('Ref/conflict.txt');selected['paths']=names
        # Old content-only capability remains capped and never implies deletions.
        await call('POST',endpoint+'/restore-plan',body={'paths':[p for p in names if p!='Ref/delete.txt']},expect=409)
        plan=await call('POST',endpoint+'/restore-plan',body=selected)
        body={**selected,'plan_sha256':plan['plan_sha256'],'request_id':str(uuid.uuid4()),'confirm':True}
        if not args.s3:
            (api.root/'exit-bulk-restore').write_text('actual process exit after two physical file applications','utf8')
            try:await call('POST',endpoint+'/restore',body=body)
            except (OSError,ValueError):pass
            else:raise AssertionError('Expected actual API process exit')
            proc=api.proc;await asyncio.to_thread(proc.wait,10);assert proc.returncode==83
            api.proc=subprocess.Popen(proc.args,cwd=api.root,env=api.env,stdout=api.log,
                stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            until=time.monotonic()+40
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
                except OSError:pass
                assert time.monotonic()<until;await asyncio.sleep(.2)
            await call('GET',api.prefix+'/raw?path=Ref/bulk-000.txt',expect=404)
            assert await call('GET',api.prefix+'/raw?path=Ref/delete.txt',raw=True)==b'DELETE'
            record('actual API exit after partial bulk application rolls back extended journal on restart')
        receipt=await call('POST',endpoint+'/restore',body=body)
        assert len(receipt['written_files'])==302 and receipt['deleted_files']==['Ref/delete.txt']
        for path in ['Ref/bulk-000.txt','Ref/bulk-299.txt','Ref/retirement.txt']:
            content=await call('GET',api.prefix+'/raw?path='+path,raw=True)
            assert hashlib.sha256(content).hexdigest()==detail['manifest']['files'][path]['sha256']
        await call('GET',api.prefix+'/raw?path=Ref/delete.txt',expect=404)
        assert await call('GET',api.prefix+'/raw?path=Ref/conflict.txt',raw=True)==b'CURRENT'
        assert await call('GET',api.prefix+'/raw?path=Ref/stable.txt',raw=True)==b'STABLE'
        await call('PUT',api.prefix+'/file',body={'path':'Ref/bulk-000.txt','content':'EDIT_AFTER_RESTORE'})
        repeated=await call('POST',endpoint+'/restore',body=body)
        assert repeated['replayed'] and repeated['request_id']==receipt['request_id']
        assert await call('GET',api.prefix+'/raw?path=Ref/bulk-000.txt',raw=True)==b'EDIT_AFTER_RESTORE'
        record('302-file atomic restore includes deletion, preserves conflict and subsequent edits on replay')
        count=await asyncio.to_thread(model_read,api,sid,rid,proof,configuration,already_restored=True)
        evidence['real_model_HTTP_calls']=count
        record('actual DeepSeek chooses file read and returns recovered sandbox nonce')
        if args.browser:
            user=await call('GET','/auth/me')
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            web=ROOT/'platform/web'
            with (api.root/'web-preview.log').open('w',encoding='utf8') as log:
                preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),
                    'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,
                    env=dict(os.environ,VITE_API_TARGET=api.base),stdout=log,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    setup={'web':'http://127.0.0.1:'+str(port),'email':user['email'],'session_id':sid,'recovery_id':rid}
                    (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),'utf8')
                    print('SANDBOX_CHANGES_BROWSER '+setup['web'],flush=True)
                    until=time.monotonic()+900
                    while not (api.root/'browser-finished.json').exists():
                        assert preview.poll() is None and api.proc.poll() is None and time.monotonic()<until
                        await asyncio.sleep(.2)
                    browser=json.loads((api.root/'browser-finished.json').read_text('utf8'))
                    assert browser['passed'];evidence['browser']=browser
                finally:
                    preview.terminate();await asyncio.to_thread(preview.wait,10)
                    evidence['preview_exited']=preview.poll() is not None
        evidence['passed']=True;save()
        return count
    try:
        if args.s3:
            from s3testsupport import ObjectStore
            with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
                await verify(store,fixture_prefix=FIXTURE+FAULT,before_sandbox=before,sandbox_command=shell,after_retirement=after)
            evidence['store_stopped']=store.proc.poll() is not None
        else:
            await verify(fixture_prefix=FIXTURE+FAULT,before_sandbox=before,sandbox_command=shell,after_retirement=after)
    except BaseException as error:
        evidence['passed']=False
        evidence['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
    finally:
        if actual_root:
            (actual_root/'changes-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n','utf8')
            print('SANDBOX_CHANGES_RESULT '+str(actual_root/'changes-result.json'),flush=True)
    return 0 if evidence['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
