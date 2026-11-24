"""Kill an actual isolated API and recover its persistent sandbox read-only."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import aiohttp

from testsupport import IsolatedServer,SERVER
from sandbox_export_checks import FIXTURE
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path


async def verify(store=None,database_url=None):
    api=IsolatedServer('sandbox-owner-disk',app='disk_owner_fixture:app',database_url=database_url)
    if store:store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
    (api.root/'disk_owner_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'+FIXTURE,encoding='utf8')
    checks=[]
    result={'passed':False,'checks':checks,'llm_calls':0,'mock_llm_calls':0,
            'storage':'s3' if store else 'fs','database':'postgresql' if database_url else 'sqlite',
            'recovery_api_implemented':False,'command_replay':False}
    try:
        with api:
            api.create_case('Persistent sandbox owner death')
            proof='API_DEATH_'+uuid.uuid4().hex+'\n'
            api.put('Ref/source.txt',proof)
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(api.base+'/ws/terminal',params={
                        'token':api.token,'case':api.case_id,'shell':'wsl-sandbox'}) as ws:
                    ready=await ws.receive_json(timeout=90);assert ready['type']=='ready',ready
                    sid=ready['session']
                    terminal=api.call('GET','/terminal/sessions/'+sid)
                    export=api.call('GET','/fixture/export/'+sid)
                    assert export['profile']['filesystem']=='ext4'
                    root=Path(export['root'])
                    assert (root/'volume.ext4').stat().st_size==128*1024*1024
                    checks.append('actual_product_terminal_uses_fixed_size_persistent_image')
                    command='mkdir Ref/recovery-empty && cp Ref/source.txt Ref/recovery-output.txt && chmod 600 Ref/recovery-output.txt && cat Ref/recovery-output.txt\r'
                    await ws.send_json({'type':'input','data':command})
                    output=b''
                    async with asyncio.timeout(30):
                        while proof.strip().encode() not in output:
                            message=await ws.receive();assert message.type==aiohttp.WSMsgType.BINARY,message
                            output+=message.data
                    checks.append('actual_sandbox_command_written_and_read_before_owner_death')
                    births=[terminal['process']['root'],*[row['identity'] for row in export['children']['children'].values()]]
                    api_birth=identity.observe(api.proc.pid)
                    (api.root/'before-death.json').write_text(json.dumps({'terminal':terminal,'export':export,'api':api_birth},indent=2))
                    api.proc.kill();await asyncio.to_thread(api.proc.wait,10)
                    assert identity.probe(api_birth)['state']=='exited'
                    checks.append('actual_api_process_killed_without_terminal_close')
                    async with asyncio.timeout(45):
                        while True:
                            observations=[]
                            for birth in births:
                                observations.append(identity.probe(birth['host']))
                                observations.append(await probe_native(birth['native']['distribution'],birth['native']['process']))
                            observations.append(await probe_native(export['profile']['distro'],terminal['process']['gate']['process']))
                            if all(item['state']=='exited' for item in observations):break
                            await asyncio.sleep(.2)
                    (api.root/'exit-observations.json').write_text(json.dumps(observations,indent=2))
                    checks.append('recorded_terminal_gate_and_exporter_host_native_births_observed_exited')
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine
            engine=create_async_engine(database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
            try:
                async with engine.connect() as connection:
                    row=(await connection.execute(text('SELECT phase,process_json FROM native_sessions WHERE id=:id'),{'id':sid})).one()
                    assert row[0]!='closed' and not json.loads(row[1]).get('export')
            finally:await engine.dispose()
            checks.append('abrupt_owner_death_does_not_record_false_successful_publication')
            expected=hashlib.sha256(proof.encode()).hexdigest()
            def inspect():
                with (api.root/'recovery.log').open('wb') as log:
                    completed=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u',
                        wsl_path(SERVER/'sandbox_owner_disk_native_checks.py'),wsl_path(root),expected],
                        stdout=log,stderr=subprocess.STDOUT,timeout=60)
                assert completed.returncode==0,'Read-only recovery failed; see '+str(api.root/'recovery.log')
            await asyncio.to_thread(inspect)
            recovered=json.loads((root/'owner-death-read.json').read_text())
            assert recovered['actual_file_sha256']==expected and not recovered['user_command_replayed']
            checks.append('real_owner_death_preserves_file_bytes_empty_directory_and_permissions')
            assert recovered['image_bytes_unchanged'] and recovered['loop_released']
            checks.append('read_only_recovery_leaves_image_unchanged_and_releases_loop')
            result.update(passed=True,recovered=recovered)
    finally:
        (api.root/'checks.json').write_text(json.dumps(result,indent=2))
        print('SANDBOX_OWNER_DISK_EVIDENCE='+str(api.root/'checks.json'),flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--storage',choices=('fs','s3'),default='fs')
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    args=parser.parse_args()
    def run(store=None,url=None,group=None):return asyncio.run(verify(store,url))
    def database(store):
        if args.database=='sqlite':return run(store)
        from mixed_read_capacity_checks import postgres
        return asyncio.run(postgres(store,'owner-death',run))
    if args.storage=='fs':database(None)
    else:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:database(store)
