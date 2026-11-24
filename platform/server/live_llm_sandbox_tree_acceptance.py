"""Actual FS/S3 tree recovery, process death, Linux modes and DeepSeek readback."""
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

import aiohttp

from live_llm_acceptance import source, FIXTURE
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from testsupport import SERVER

ROOT = SERVER.parents[1]
FAULT = r'''
from app.services import tree_transactions, s3_file_transactions
_move_tree = tree_transactions._move
def crash_tree_gap(source, destination, **kwargs):
 result = _move_tree(source, destination, **kwargs)
 flag = Path(__file__).parent/'exit-tree-restore'
 if flag.exists() and destination.parent.name == 'before':
  flag.unlink(); os._exit(84)
 return result
tree_transactions._move = crash_tree_gap
_remote_apply = s3_file_transactions._apply
def crash_remote_tree(*args, **kwargs):
 result = _remote_apply(*args, **kwargs)
 flag = Path(__file__).parent/'exit-tree-restore'
 if flag.exists() and args[2] == 0 and args[4] == 'after':
  flag.unlink(); os._exit(85)
 return result
s3_file_transactions._apply = crash_remote_tree
'''


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    for name, digest in json.loads(args.review.read_text('utf8'))['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    configuration = await source(args.source_profile_id)
    evidence = {'passed': False, 'mock_llm_calls': 0, 'checks': [], 'storage': 's3' if args.s3 else 'fs'}
    actual_root = None
    async def before(api):
        nonlocal actual_root
        actual_root = api.root
        print('TREE_ROOT '+str(api.root), flush=True)
        for path, content in [('Ref/file-to-dir','ORIGINAL'), ('Ref/dir-to-file/old.txt','OLD'),
                              ('Ref/remove-dir/old.txt','DELETE'), ('Ref/mode.sh','#!/bin/sh\necho MODE_OK\n')]:
            await asyncio.to_thread(api.call, 'PUT', api.prefix+'/file', body={'path':path,'content':content})

    command = """from pathlib import Path
import shutil
p=Path('Ref')
(p/'new-empty').mkdir(); (p/'new-empty').chmod(0o701)
(p/'file-to-dir').unlink(); (p/'file-to-dir').mkdir(); (p/'file-to-dir').chmod(0o700)
(p/'file-to-dir'/'child.txt').write_text('CHILD'); (p/'file-to-dir'/'child.txt').chmod(0o600)
shutil.rmtree(p/'dir-to-file'); (p/'dir-to-file').write_text('REPLACED'); (p/'dir-to-file').chmod(0o640)
shutil.rmtree(p/'remove-dir'); (p/'mode.sh').chmod(0o750)
"""

    async def after(api, sid, rid, proof):
        async def call(method, path, **kw):return await asyncio.to_thread(api.call, method, path, **kw)
        def save():(api.root/'tree-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n','utf8')
        def record(name):evidence['checks'].append(name);save();print('TREE_PASS '+name,flush=True)
        endpoint=f'/terminal/sessions/{sid}/recoveries/{rid}'
        inventory=await call('GET',endpoint+'/tree-changes')
        names=[e['path'] for e in inventory['files'] if not e['protected']]
        assert {'Ref/new-empty','Ref/file-to-dir','Ref/dir-to-file','Ref/remove-dir','Ref/mode.sh'}<=set(names)
        selected={'paths':names,'semantics':'sandbox_tree_changes'}
        async def preview():return await call('POST',endpoint+'/restore-plan',body=selected)
        def confirmation(plan):return {**selected,'plan_sha256':plan['plan_sha256'],'confirm':True,'request_id':str(uuid.uuid4())}
        await call('PUT',api.prefix+'/file',body={'path':'Ref/remove-dir/new-user.txt','content':'PRESERVE'})
        plan=await preview()
        assert any(e['action']=='conflict' for e in plan['files'])
        await call('POST',endpoint+'/restore',body=confirmation(plan),expect=409)
        assert await call('GET',api.prefix+'/raw?path=Ref/remove-dir/new-user.txt',raw=True)==b'PRESERVE'
        await call('DELETE',api.prefix+'/file?path=Ref/remove-dir/new-user.txt')
        await call('PUT',api.prefix+'/file',body={'path':'Ref/user.txt','content':'PRESERVE'})
        plan=await preview()
        assert not any(e['action']=='conflict' for e in plan['files'])
        assert len([e for e in plan['files'] if e['action']=='replace_type'])==2
        record('new user child prevents destructive directory replacement before any writes')
        body=confirmation(plan)
        (api.root/'exit-tree-restore').write_text('actual FS rename gap or S3 first applied object', 'utf8')
        try:await call('POST',endpoint+'/restore',body=body)
        except (OSError,ValueError):pass
        else:raise AssertionError('Actual API death was required')
        proc=api.proc;await asyncio.to_thread(proc.wait,10)
        assert proc.returncode==(85 if args.s3 else 84)
        api.proc=subprocess.Popen(proc.args,cwd=api.root,env=api.env,stdout=api.log,
                                  stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        until=time.monotonic()+40
        while True:
            assert api.proc.poll() is None
            try:
                if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
            except OSError:pass
            assert time.monotonic()<until;await asyncio.sleep(.2)
        assert await call('GET',api.prefix+'/raw?path=Ref/file-to-dir',raw=True)==b'ORIGINAL'
        assert await call('GET',api.prefix+'/raw?path=Ref/dir-to-file/old.txt',raw=True)==b'OLD'
        await call('GET',endpoint+'/restores/'+body['request_id'],expect=404)
        record('actual API death rolls back directory gap or partially applied S3 tree')
        receipt=await call('POST',endpoint+'/restore',body=body)
        assert receipt['directories_restored'] and receipt['permissions_restored']
        listing=await call('GET',api.prefix+'/tree')
        assert any(e['path']=='Ref/new-empty' and e.get('kind')=='directory' for e in listing['entries'])
        assert not any(e['path'].startswith('Ref/remove-dir') for e in listing['entries'])
        assert await call('GET',api.prefix+'/raw?path=Ref/file-to-dir/child.txt',raw=True)==b'CHILD'
        assert await call('GET',api.prefix+'/raw?path=Ref/dir-to-file',raw=True)==b'REPLACED'
        assert await call('GET',api.prefix+'/raw?path=Ref/user.txt',raw=True)==b'PRESERVE'
        await call('PUT',api.prefix+'/file',body={'path':'Ref/user.txt','content':'LATER'})
        assert (await call('POST',endpoint+'/restore',body=body))['replayed']
        assert await call('GET',api.prefix+'/raw?path=Ref/user.txt',raw=True)==b'LATER'
        record('empty directories, deletion, both type swaps and independent current files restored; replay is inert')

        # Exercise the next real Linux sandbox. Metadata alone is not mode proof.
        check="""from pathlib import Path
import stat
expected={'Ref/new-empty':0o701,'Ref/file-to-dir':0o700,'Ref/file-to-dir/child.txt':0o600,'Ref/dir-to-file':0o640,'Ref/mode.sh':0o750}
for p,m in expected.items(): assert stat.S_IMODE(Path(p).stat().st_mode)==m,(p,oct(Path(p).stat().st_mode))
assert Path('Ref/new-empty').is_dir() and not list(Path('Ref/new-empty').iterdir())
assert Path('Ref/file-to-dir').is_dir() and Path('Ref/dir-to-file').is_file()
assert not Path('Ref/remove-dir').exists()
print('TREE_MODES_'+'VERIFIED',flush=True)
"""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as client:
            async with client.ws_connect(api.base+'/ws/terminal', headers={'Authorization': 'Bearer ' + api.token},
                                         params={'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                ready=await ws.receive_json(timeout=90);assert ready['type']=='ready',ready
                second=ready['session']
                await ws.send_json({'type':'input','data':'python3 -c '+shlex.quote(check)+'; exit\r'})
                output=b''
                async with asyncio.timeout(90):
                    while True:
                        msg=await ws.receive()
                        if msg.type==aiohttp.WSMsgType.BINARY:output+=msg.data;assert len(output)<1024*1024
                        elif msg.type==aiohttp.WSMsgType.TEXT:
                            event=json.loads(msg.data)
                            if event['type']=='exit':assert event['code']==0;break
                            assert event['type']!='error',event
                        else:raise AssertionError('Terminal closed without normal exit')
                assert b'TREE_MODES_VERIFIED' in output
                state=await call('GET','/terminal/sessions/'+second)
                assert state['cleanup_verified'] is True
        evidence['second_sandbox_session']=second
        record('next actual Linux sandbox reconstructs empty directories and all five exact POSIX modes')
        count=await asyncio.to_thread(model_read,api,sid,rid,proof,configuration,already_restored=True)
        evidence['real_model_HTTP_calls']=count
        record('actual DeepSeek chooses read_text_file and returns recovered random sandbox output')
        if args.browser:
            user=await call('GET','/auth/me')
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            web=ROOT/'platform/web'
            with (api.root/'web-preview.log').open('w',encoding='utf8') as log:
                preview_process=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),
                    'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,
                    env=dict(os.environ,VITE_API_TARGET=api.base),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    setup={'web':'http://127.0.0.1:'+str(port),'email':user['email'],'session_id':sid,'recovery_id':rid,'case_id':api.case_id}
                    (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2),'utf8')
                    print('TREE_BROWSER '+setup['web'],flush=True)
                    until=time.monotonic()+900
                    while not (api.root/'browser-finished.json').exists():
                        assert preview_process.poll() is None and api.proc.poll() is None and time.monotonic()<until
                        await asyncio.sleep(.2)
                    browser=json.loads((api.root/'browser-finished.json').read_text('utf8'))
                    assert browser['passed'];evidence['browser']=browser
                finally:
                    preview_process.terminate();await asyncio.to_thread(preview_process.wait,10)
                    evidence['preview_exited']=preview_process.poll() is not None
        evidence['passed']=True;save();return count
    try:
        options=dict(fixture_prefix=FIXTURE+FAULT,before_sandbox=before,
                     sandbox_command='python3 -c '+shlex.quote(command)+'; ',after_retirement=after)
        if args.s3:
            from s3testsupport import ObjectStore
            with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
                await verify(store,**options)
            evidence['store_stopped']=store.proc.poll() is not None
        else:await verify(**options)
    except BaseException as error:
        evidence['passed']=False
        evidence['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
    finally:
        if actual_root:
            (actual_root/'tree-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n','utf8')
            print('TREE_RESULT '+str(actual_root/'tree-result.json'),flush=True)
    return 0 if evidence['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
