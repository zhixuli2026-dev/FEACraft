"""Actual normal WSL closure publishes a tree/revision, then DeepSeek reads it."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import time

import aiohttp

from live_llm_acceptance import FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_recovery_ui_cleanup import verify as verify_cleanup
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
REPLAY = '''
from app.services import sandbox_export_publication
original_publish = sandbox_export_publication.publish
async def repeat_publication(*args, **kwargs):
 first = await original_publish(*args, **kwargs)
 second = await original_publish(*args, **kwargs)
 assert first == second, 'Committed export retry changed its receipt'
 return second
sandbox_export_publication.publish = repeat_publication
'''


async def verify(configuration, store=None, *, after_publish=None, fixture_extra='', next_sandbox_check=''):
    api = IsolatedServer('sandbox-recovery-ui-publication', app='publication_fixture:app')
    (api.root/'publication_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE+REPLAY+fixture_extra, 'utf8')
    (api.root/'ready.json').write_text(json.dumps({'root':str(api.root)}), 'utf8')
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status':'Enabled'})
    result = {'passed':False, 'storage':'s3' if store else 'fs', 'mock_llm_calls':0, 'checks':[]}
    def save():
        (api.root/'publication-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', 'utf8')
    def record(name):
        result['checks'].append(name); save(); print('PUBLICATION_PASS '+name, flush=True)
    async def call(method, path, **kw):
        deadline = time.monotonic()+30
        while True:
            try:
                return await asyncio.to_thread(api.call, method, path, **kw)
            except AssertionError as error:
                # This is admission refusal before the API writes anything.
                # Never replay arbitrary failures or actual LLM messages.
                if ('"code":"storage_quota_busy"' not in str(error).replace(' ','')
                        or time.monotonic()>=deadline):
                    raise
                result['admission_retries']=result.get('admission_retries',0)+1
                await asyncio.sleep(.2)
    async def shell(code, *, during=None, expected_error=False):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as client:
            async with client.ws_connect(api.base+'/ws/terminal', headers={'Authorization': 'Bearer ' + api.token},
                                         params={'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                ready = await ws.receive_json(timeout=90)
                assert ready['type']=='ready', ready
                sid = ready['session']
                await ws.send_json({'type':'input', 'data':'python3 -c '+shlex.quote(code+'\nprint("PUBLICATION_"+"READY",flush=True)')+'\r'})
                output = b''
                async with asyncio.timeout(45):
                    while b'PUBLICATION_READY' not in output:
                        message = await ws.receive()
                        assert message.type==aiohttp.WSMsgType.BINARY, str(message.type)
                        output += message.data
                        assert len(output)<1024*1024
                assert b'Traceback (most recent call last)' not in output, output.decode('utf8','replace')[-2000:]
                if during is not None:
                    await during()
                await ws.send_json({'type':'input', 'data':'exit\r'})
                async with asyncio.timeout(90):
                    while True:
                        message = await ws.receive()
                        if message.type==aiohttp.WSMsgType.TEXT:
                            event=json.loads(message.data)
                            if event['type']=='exit':
                                assert event['code']==0
                                break
                            if event['type']=='error':
                                assert expected_error, event
                                break
                        elif message.type!=aiohttp.WSMsgType.BINARY:
                            assert expected_error
                            break
                async with asyncio.timeout(20):
                    while True:
                        state=await call('GET','/terminal/sessions/'+sid)
                        if state['phase'] in {'closed','failed'}:break
                        await asyncio.sleep(.1)
                assert state['cleanup_verified'] is True
                assert bool(state['error'])==expected_error, state['error']
                return sid
    try:
        with api:
            api.create_case('Normal sandbox publication')
            original=await call('GET',api.prefix+'/caseDict')
            for path, content in [('Ref/file-to-dir','ORIGINAL'),('Ref/dir-to-file/old','OLD'),
                                  ('Ref/remove/old','DELETE'),('Ref/conflict.txt','BEFORE')]:
                await call('PUT',api.prefix+'/file',body={'path':path,'content':content})
            sid=await shell('''from pathlib import Path
import shutil, secrets
p=Path('Ref')
(p/'empty').mkdir(); (p/'empty').chmod(0o701)
(p/'file-to-dir').unlink(); (p/'file-to-dir').mkdir(); (p/'file-to-dir').chmod(0o700)
(p/'file-to-dir'/'child').write_text('CHILD'); (p/'file-to-dir'/'child').chmod(0o600)
shutil.rmtree(p/'dir-to-file'); (p/'dir-to-file').write_text('REPLACED'); (p/'dir-to-file').chmod(0o640)
shutil.rmtree(p/'remove')
(p/'proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')
d=Path('0-caseDict/caseDict'); d.write_text(d.read_text()+'\\n// normal sandbox publication\\n')
''')
            current=await call('GET',api.prefix+'/caseDict')
            assert current['dict_revision']==original['dict_revision']+1
            assert 'normal sandbox publication' in current['content']
            assert await call('GET',api.prefix+'/raw?path=Ref/file-to-dir/child',raw=True)==b'CHILD'
            assert await call('GET',api.prefix+'/raw?path=Ref/dir-to-file',raw=True)==b'REPLACED'
            entries=(await call('GET',api.prefix+'/tree'))['entries']
            assert any(e['path']=='Ref/empty' and e.get('kind')=='directory' for e in entries)
            assert not any(e['path'].startswith('Ref/remove') for e in entries)
            with sqlite3.connect(api.root/'test.sqlite') as db:
                receipts=[json.loads(r[0]) for r in db.execute("SELECT payload_json FROM durable_events WHERE type='terminal.export_published'")]
            assert len(receipts)==1 and receipts[0]['session_id']==sid
            for path in ('case-events.jsonl','provenance.jsonl'):
                history=(await call('GET',api.prefix+'/raw?path=0-caseDict/'+path,raw=True)).decode()
                assert history.count('sandbox-export')==1
            record('normal close publishes empty directory, deletion, type swaps and one atomic dictionary revision; retry is inert')
            if after_publish is not None:
                await after_publish(api)
                current=await call('GET',api.prefix+'/caseDict')
            await shell('''from pathlib import Path
import stat
for p,m in {'Ref/empty':0o701,'Ref/file-to-dir':0o700,'Ref/file-to-dir/child':0o600,'Ref/dir-to-file':0o640}.items():
 assert stat.S_IMODE(Path(p).stat().st_mode)==m,(p,oct(Path(p).stat().st_mode))
assert Path('Ref/empty').is_dir() and not list(Path('Ref/empty').iterdir())
assert not Path('Ref/remove').exists()
'''+next_sandbox_check)
            assert (await call('GET',api.prefix+'/caseDict'))['dict_revision']==current['dict_revision']
            record('next real Linux sandbox retains four exact modes and read-only close creates no dictionary revision')
            proof_bytes=await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
            result['real_model_HTTP_calls']=await asyncio.to_thread(model_read,api,sid,'',proof_bytes.decode().strip(),configuration,
                already_restored=True,path='Ref/proof.txt',expected_content=proof_bytes)
            record('real DeepSeek chooses read_text_file and reads unpredictable normally published shell output')
            async def concurrent_edit():
                await call('PUT',api.prefix+'/file',body={'path':'Ref/conflict.txt','content':'USER'})
            await shell("from pathlib import Path; Path('Ref/conflict.txt').write_text('SHELL'); Path('Ref/rejected.txt').write_text('NO')",
                        during=concurrent_edit,expected_error=True)
            assert await call('GET',api.prefix+'/raw?path=Ref/conflict.txt',raw=True)==b'USER'
            await call('GET',api.prefix+'/raw?path=Ref/rejected.txt',expect=404)
            record('concurrent actual API edit rejects the entire normal export without overwriting user data')
            await shell("from pathlib import Path; p=Path('0-caseDict/caseDict'); p.write_text(p.read_text().replace('workflowState A;', 'workflowState V;')); Path('Ref/rejected.txt').write_text('NO')",
                        expected_error=True)
            assert (await call('GET',api.prefix+'/caseDict'))['dict_revision']==current['dict_revision']
            await call('GET',api.prefix+'/raw?path=Ref/rejected.txt',expect=404)
            record('ordinary shell configuration change cannot silently advance workflow or partially publish other files')
            result['passed']=True
    except BaseException as error:
        result['passed']=False
        result['failure']={'type':type(error).__name__, 'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1600]}
    finally:
        if (api.root/'test.sqlite').exists():
            try:await verify_cleanup(api.root)
            except Exception as error:
                result['passed']=False;result['cleanup_failure']=type(error).__name__
        result['api_stopped']=api.proc is not None and api.proc.poll() is not None
        save(); print('PUBLICATION_RESULT '+str(api.root/'publication-result.json'), flush=True)
    return result['passed']


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args=parser.parse_args()
    for name, digest in json.loads(args.review.read_text('utf8'))['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest, name
    configuration=await source(args.source_profile_id)
    if args.s3:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
            passed=await verify(configuration,store)
        assert store.proc.poll() is not None
    else:passed=await verify(configuration)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
