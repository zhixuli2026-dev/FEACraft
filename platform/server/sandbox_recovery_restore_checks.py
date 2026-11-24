"""Actual WSL recovery then real HTTP confirmed file restoration. No LLM."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from testsupport import IsolatedServer, SERVER
from sandbox_recovery_ui_cleanup import verify as verify_cleanup
from app.models import Case, NativeRequest, DurableEvent


async def verify(store=None, *, payload_mib=0):
    api = IsolatedServer('sandbox-recovery-ui-restore', app='restore_fixture:app')
    fixture = api.root/'restore_fixture.py'
    native_temp = api.root/'native-temp'
    native_temp.mkdir()
    configuration = store.settings() if store else {}
    configuration.update(TEMP=str(native_temp), TMP=str(native_temp), TMPDIR=str(native_temp))
    fixture.write_text('import os\nos.environ.update('+repr(configuration)+')\nfrom app.main import app\n', encoding='utf-8')
    (api.root/'ready.json').write_text(json.dumps({'root':str(api.root)}))
    report={'passed':False,'storage':'s3' if store else 'fs','llm_calls':0,'mock_llm_calls':0,'checks':[]}
    engine=create_async_engine('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        (api.root/'restore-checks.json').write_text(json.dumps(report,indent=2))
        print('RESTORE_PASS '+name,flush=True)
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    async def restart():
        args=api.proc.args
        api.proc.kill();await asyncio.to_thread(api.proc.wait,10)
        api.proc=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
                except OSError:pass
                await asyncio.sleep(.1)
    try:
        if store:
            async with aiohttp.ClientSession(trust_env=False) as anonymous:
                async with anonymous.get(f'{store.endpoint}/{store.bucket}?list-type=2') as response:
                    assert response.status == 403, response.status
            allowed = store.raw_client.list_objects_v2(Bucket=store.bucket)['ResponseMetadata']['HTTPStatusCode']
            assert allowed == 200
            report['authentication'] = {'anonymous':403, 'signed':allowed}
            report['store_receipt'] = str(store.root/'store.json')
            store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status':'Enabled'})
        with api:
            case=api.create_case('Confirmed recovery file content')
            paths=['Ref/existing.txt','Ref/restored.txt']
            await call('PUT',api.prefix+'/file',body={'path':paths[0],'content':'original project'})
            suffix=uuid.uuid4().hex
            proof='REAL_RESTORE_'+suffix
            first_line=(proof+'\n').encode()
            expected=hashlib.sha256(first_line)
            zeroes=bytes(1024*1024)
            for _ in range(payload_mib):
                expected.update(zeroes)
            file_record={'size':len(first_line)+payload_mib*len(zeroes),'sha256':expected.hexdigest()}
            report['snapshot_file'] = file_record
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as client:
                async with client.ws_connect(api.base+'/ws/terminal',params={'token':api.token,'case':api.case_id,'shell':'wsl-sandbox'}) as ws:
                    ready=await ws.receive_json(timeout=90);assert ready['type']=='ready',ready
                    sid=ready['session']
                    marker = "printf '%s%s\\n' 'REAL_RESTORE_' '"+suffix+"'"
                    command = marker + ' > Ref/restored.txt'
                    if payload_mib:
                        command += f' && dd if=/dev/zero bs=1048576 count={payload_mib} status=none >> Ref/restored.txt'
                    command += (' && cp Ref/restored.txt Ref/existing.txt && chmod 600 Ref/restored.txt'
                                ' && mkdir Ref/restore-empty && ' + marker)
                    await ws.send_json({'type':'input','data':command+'\r'})
                    output=b''
                    async with asyncio.timeout(35):
                        while proof.encode() not in output:
                            message=await ws.receive();assert message.type==aiohttp.WSMsgType.BINARY,message
                            output+=message.data;assert len(output)<1024*1024
                    await restart()
                async with asyncio.timeout(60):
                    while True:
                        result=await call('GET','/terminal/sessions/'+sid+'/recovery-inspection')
                        if result['status']=='processes_exited':break
                        await asyncio.sleep(.2)
                async with client.post(api.base+'/api/v1/terminal/sessions/'+sid+'/recoveries',headers={'Authorization':'Bearer '+api.token}) as response:
                    result=await response.json();assert response.status==201,(response.status,result)
            rid=result['id'];endpoint='/terminal/sessions/'+sid+'/recoveries/'+rid
            record('actual_sandbox_output_and_snapshot_after_api_death')
            preview=await call('POST',endpoint+'/restore-plan',body={'paths':paths})
            assert [r['action'] for r in preview['files']]==['overwrite','create']
            assert all(item['snapshot'] == file_record for item in preview['files']), preview
            assert not preview['directories_restored'] and not preview['permissions_restored'] and not preview['deletions_applied']
            assert await call('GET',api.prefix+'/raw?path='+paths[0],raw=True)==b'original project'
            await call('GET',api.prefix+'/raw?path='+paths[1],expect=404)
            record('preview_compares_actual_current_files_without_writing')
            request_id=str(uuid.uuid4())
            body={'paths':paths,'request_id':request_id,'plan_sha256':preview['plan_sha256'],'confirm':True,'semantics':'file_contents_only'}
            await call('POST',endpoint+'/restore',body={k:v for k,v in body.items() if k!='confirm'},expect=422)
            await call('POST',endpoint+'/restore',body={**body,'confirm':False},expect=422)
            record('explicit_confirmation_required','protocol_fault')
            await call('PUT',api.prefix+'/file',body={'path':paths[1],'content':'edited after preview'})
            stale=await call('POST',endpoint+'/restore',body=body,expect=409)
            assert stale['detail']['code']=='recovery_plan_stale'
            assert await call('GET',api.prefix+'/raw?path='+paths[0],raw=True)==b'original project'
            assert await call('GET',api.prefix+'/raw?path='+paths[1],raw=True)==b'edited after preview'
            record('real_concurrent_edit_rejects_entire_stale_plan_without_partial_write')
            preview=await call('POST',endpoint+'/restore-plan',body={'paths':paths})
            body['plan_sha256']=preview['plan_sha256']
            restored=await call('POST',endpoint+'/restore',body=body)
            assert sorted(restored['written_files'])==paths and not restored['replayed']
            for path in paths:
                actual = await call('GET',api.prefix+'/raw?path='+path,raw=True)
                assert len(actual) == file_record['size'] and hashlib.sha256(actual).hexdigest() == file_record['sha256']
            record('confirmed_atomic_file_restore_writes_real_snapshot_bytes')
            receipt=await call('GET',endpoint+'/restores/'+request_id)
            assert receipt['file_transaction_id']==restored['file_transaction_id']
            await call('PUT',api.prefix+'/file',body={'path':paths[1],'content':'later independent edit'})
            replay=await call('POST',endpoint+'/restore',body=body)
            assert replay['replayed'] and replay['file_transaction_id']==restored['file_transaction_id']
            assert await call('GET',api.prefix+'/raw?path='+paths[1],raw=True)==b'later independent edit'
            record('same_confirmation_returns_receipt_without_rewriting_later_edit')
            conflict=await call('POST',endpoint+'/restore',body={**body,'paths':[paths[0]]},expect=409)
            assert conflict['detail']['code']=='recovery_request_conflict'
            await call('POST',endpoint+'/restore-plan',body={'paths':['0-caseDict/caseDict']},expect=409)
            await call('POST',endpoint+'/restore-plan',body={'paths':['Ref/restore-empty']},expect=409)
            await call('POST',endpoint+'/restore-plan',body={'paths':['../outside']},expect=409)
            record('reused_confirmation_and_protected_nonfile_escape_paths_refused','protocol_fault')
            async with engine.connect() as db:
                row=(await db.execute(select(NativeRequest.__table__).where(NativeRequest.id==rid))).mappings().one()
            source=Path(json.loads(row['profile_json'])['root'])/'files'/paths[1]
            preview=await call('POST',endpoint+'/restore-plan',body={'paths':paths})
            with source.open('r+b') as stream:
                original=stream.read(1)
                assert original == first_line[:1]
                stream.seek(0)
                stream.write(bytes([original[0] ^ 1]))
            try:
                rejected=await call('POST',endpoint+'/restore',body={**body,'request_id':str(uuid.uuid4()),'plan_sha256':preview['plan_sha256']},expect=409)
                assert rejected['detail']['code']=='recovery_evidence_invalid'
                assert await call('GET',api.prefix+'/raw?path='+paths[1],raw=True)==b'later independent edit'
            finally:
                with source.open('r+b') as stream:
                    stream.write(original)
            record('actual_same_size_corrupt_snapshot_file_refused_before_project_mutation','protocol_fault')
            stranger=await call('POST','/auth/register',body={'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other owner'})
            await call('POST',endpoint+'/restore-plan',body={'paths':paths},extra_headers={'Authorization':'Bearer '+stranger['token']},expect=404)
            await call('POST',endpoint+'/restore',body=body,extra_headers={'Authorization':''},expect=401)
            await call('GET',endpoint+'/restores/'+request_id,extra_headers={'Authorization':'Bearer '+stranger['token']},expect=404)
            record('actual_authentication_and_owner_checks_cover_preview_write_receipt')
            await restart()
            assert await call('GET',endpoint+'/restores/'+request_id)==receipt
            replay=await call('POST',endpoint+'/restore',body=body)
            assert replay['replayed']
            assert await call('GET',api.prefix+'/raw?path='+paths[1],raw=True)==b'later independent edit'
            async with engine.connect() as db:
                rows=list(await db.execute(select(DurableEvent.__table__).where(DurableEvent.id==request_id)))
                assert len(rows)==1
            record('actual_restart_retains_unique_confirmation_and_does_not_repeat_write')
            async with engine.connect() as db:
                original_prefix=await db.scalar(select(Case.s3_prefix).where(Case.id==api.case_id))
            try:
                async with engine.begin() as db:
                    await db.execute(update(Case).where(Case.id==api.case_id).values(s3_prefix=original_prefix+'-changed'))
                changed=await call('POST',endpoint+'/restore-plan',body={'paths':paths},expect=409)
                assert changed['detail']['code']=='case_source_changed'
                await call('POST',endpoint+'/restore',body=body,expect=409)
            finally:
                async with engine.begin() as db:
                    await db.execute(update(Case).where(Case.id==api.case_id).values(s3_prefix=original_prefix))
            record('changed_case_storage_source_refused_for_preview_and_restore','protocol_fault')
            await call('GET',endpoint+'/restores/'+str(uuid.uuid4()),expect=404)
        await verify_cleanup(api.root)
        record('all_original_process_identities_absent_after_test')
        report['passed']=True
    finally:
        await engine.dispose()
        fixture.unlink(missing_ok=True)
        (api.root/'restore-checks.json').write_text(json.dumps(report,indent=2))
        print('RESTORE_EVIDENCE='+str(api.root/'restore-checks.json'),flush=True)


if __name__=='__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage',choices=['fs','s3'],default='fs')
    parser.add_argument('--payload-mib',type=int,choices=(0,6),default=0)
    args=parser.parse_args()
    if args.storage=='s3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',
                         volume_max=12, authenticated=True) as store:
            asyncio.run(verify(store, payload_mib=args.payload_mib))
        assert store.proc.poll() == 0
    else:
        asyncio.run(verify(payload_mib=args.payload_mib))
