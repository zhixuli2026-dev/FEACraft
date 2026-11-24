"""Actual WSL/PTY/export ownership checks; no model provider or fake inference.

The fixture observes the production lifecycle without replacing its results.
"""
import asyncio
import json
from pathlib import Path
import uuid

import aiohttp

from testsupport import IsolatedServer, SERVER

FIXTURE = '''
import hashlib,json
from pathlib import Path
from app.main import app
from app.db import SessionLocal
from app.models import NativeRequest
from app.services.terminal_sessions import TerminalSession
from app.services.sandbox_export_execution import SandboxExport
from app.services import native_request_records
original_start=TerminalSession.start
exports={}
async def start(session,**kwargs):
 original_finalize=kwargs.get('finalize')
 async def observed_finalize(current):
  try:return await original_finalize(current)
  except BaseException:
   import traceback
   (Path(__file__).parent/'finalize-error.txt').write_text(traceback.format_exc(),encoding='utf8')
   raise
 kwargs['finalize']=observed_finalize
 await original_start(session,**kwargs)
 exports[session.id]=session.exporter
 return session
TerminalSession.start=start
@app.get('/api/v1/fixture/export/{identifier}')
async def evidence(identifier:str):
 exporter=exports[identifier]
 async with SessionLocal() as db:
  row=await db.get(NativeRequest,exporter.request_id)
  result=native_request_records.public(row)
  result['profile']=json.loads(row.profile_json)
 result['manifest']=exporter.manifest
 result['initial']=exporter.initial
 result['root']=str(exporter.root)
 result['helper_task_done']=exporter.task.done()
 files={}
 if exporter.manifest and (exporter.root/'files').is_dir():
  for name,entry in exporter.manifest['files'].items():
   raw=(exporter.root/'files'/name).read_bytes()
   files[name]={'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
 result['actual_files']=files
 return result
'''


async def verify(mode='forced', store=None, database_url=None):
    api = IsolatedServer('sandbox-export-owned', app='sandbox_export_fixture:app', database_url=database_url)
    if store:store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root / 'sandbox_export_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE, encoding='utf8')
    checks = []
    result = {'passed': False, 'mode': mode, 'storage': 's3' if store else 'fs',
              'database': 'postgresql' if database_url else 'sqlite', 'checks': checks, 'llm_calls': 0,
              'mock_llm_calls': 0, 'product_publication_integrated': True}
    try:
        with api:
            api.create_case('Registered sandbox export')
            proof = 'OWNED_EXPORT_' + uuid.uuid4().hex
            api.put('Ref/source.txt', proof + '\n')
            api.put('Ref/remove.txt', 'remove me')
            api.put('Ref/update.txt', 'old version')
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(api.base + '/ws/terminal', params={
                        'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                    ready = await ws.receive_json(timeout=90)
                    assert ready['type'] == 'ready', ready
                    sid = ready['session']
                    try:
                        before = api.call('GET', '/fixture/export/' + sid)
                        assert before['phase'] == 'active' and before['initial'] and not before['helper_task_done'], before
                        child = list(before['children']['children'].values())
                        assert len(child) == 1 and child[0]['phase'] == 'running', child
                        assert child[0]['identity']['runtime'] == 'wsl' and child[0]['purpose'] == 'sandbox_export'
                        checks.append('independent_export_child_registered_and_pinned_before_input')
                        command = 'rm Ref/remove.txt && cp Ref/source.txt Ref/update.txt && cp Ref/source.txt Ref/output.txt && cat Ref/output.txt'
                        if mode == 'invalid':
                            command = 'ln -s /etc/passwd Ref/forbidden && ' + command
                        await ws.send_json({'type': 'input', 'data': command + '\r'})
                        output = b''
                        async with asyncio.timeout(30):
                            while proof.encode() not in output:
                                message = await ws.receive()
                                assert message.type == aiohttp.WSMsgType.BINARY, message
                                output += message.data
                        checks.append('actual_sandbox_command_produced_unique_file_bytes')
                        if mode=='conflict':
                            async with asyncio.timeout(15):
                                while True:
                                    response=api.call('PUT',api.prefix+'/file',{'path':'Ref/output.txt','content':'concurrent edit'},expect=(200,409))
                                    if 'detail' not in response:break
                                    assert response['detail']['code']=='storage_quota_busy',response
                                    await asyncio.sleep(.1)
                        if mode == 'normal':
                            await ws.send_json({'type': 'input', 'data': 'exit\r'})
                            async with asyncio.timeout(60):
                                while True:
                                    closed = api.call('GET', '/terminal/sessions/' + sid)
                                    if closed['phase'] in {'closed', 'failed'}:
                                        break
                                    await asyncio.sleep(.1)
                        else:
                            api.call('DELETE', '/terminal/sessions/' + sid, expect=502 if mode in {'invalid','conflict'} else 200)
                            closed = api.call('GET', '/terminal/sessions/' + sid)
                        assert closed['cleanup_verified'] is True, closed
                        checks.append('actual_terminal_full_scope_exit_verified')
                        evidence = api.call('GET', '/fixture/export/' + sid)
                        (api.root / 'export-evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
                        (api.root / 'terminal-evidence.json').write_text(json.dumps(closed, indent=2), encoding='utf8')
                        assert evidence['cleanup_verified'] is True and evidence['helper_task_done'], evidence
                        checks.append('export_helper_exit_verified_and_durable_request_settled')
                        if mode == 'invalid':
                            assert closed['phase'] == 'failed' and evidence['phase'] == 'failed' and evidence['manifest'] is None
                            assert not (Path(evidence['root']) / 'complete.json').exists()
                            checks.append('actual_symlink_rejected_without_success_receipt')
                        elif mode=='conflict':
                            assert closed['phase']=='failed' and evidence['phase']=='succeeded'
                            assert not closed['process'].get('export')
                            assert api.call('GET',api.prefix+'/raw?path=Ref/output.txt',raw=True)==b'concurrent edit'
                            assert api.call('GET',api.prefix+'/raw?path=Ref/update.txt',raw=True)==b'old version'
                            assert api.call('GET',api.prefix+'/raw?path=Ref/remove.txt',raw=True)==b'remove me'
                            checks.append('concurrent_source_edit_preserved_without_partial_publication')
                        else:
                            assert closed['phase'] == 'closed' and evidence['phase'] == 'succeeded', evidence
                            if not store:
                                assert evidence['actual_files'] == evidence['manifest']['files']
                                assert evidence['actual_files']['Ref/source.txt'] == evidence['actual_files']['Ref/output.txt']
                                checks.append('all_exported_files_hash_verified_after_terminal_exit')
                            saved=api.call('GET', api.prefix+'/raw?path=Ref/output.txt', raw=True)
                            assert saved==(proof+'\n').encode(), saved
                            assert closed['process']['export']['request_id']==evidence['id']
                            assert api.call('GET',api.prefix+'/raw?path=Ref/update.txt',raw=True)==(proof+'\n').encode()
                            api.call('GET',api.prefix+'/raw?path=Ref/remove.txt',expect=404)
                            checks.append('actual_case_api_returns_saved_output_with_durable_publication_receipt')
                            checks.append('existing_file_update_and_deletion_are_published')
                    finally:
                        api.call('DELETE', '/terminal/sessions/' + sid, expect=(200, 502))
            result['passed'] = True
    finally:
        (api.root / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        print('SANDBOX_EXPORT_EVIDENCE=' + str(api.root / 'checks.json'), flush=True)


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('forced', 'normal', 'invalid', 'conflict'), default='forced')
    parser.add_argument('--storage',choices=('fs','s3'),default='fs')
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    args=parser.parse_args()
    def run(store=None,url=None,group=None):return asyncio.run(verify(group or args.mode,store,url))
    def database(store):
        if args.database=='sqlite':return run(store)
        from mixed_read_capacity_checks import postgres
        return asyncio.run(postgres(store,args.mode,run))
    if args.storage=='fs':database(None)
    else:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:database(store)
