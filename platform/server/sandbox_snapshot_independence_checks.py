"""Actual original-image removal from its registered path; retained snapshot use.

The original source is renamed into checked isolated quarantine and restored in
finally. This verifies cleanup compatibility, not a product cleanup operation.
No model calls in this entry; optional live continuation is supplied only by the
explicit live_llm_sandbox_recovery_acceptance entrypoint.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import NativeRequest
from sandbox_retirement_checks import verify
from testsupport import SERVER


async def without_image(api,sid,rid,proof,continuation=None):
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    report={'passed':False,'actual_source_directory_absent':False,'product_cleanup_implemented':False,
        'checks':[],'mock_llm_calls':0,'llm_calls':0}
    target=api.root/'snapshot-independence.json'
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        target.write_text(json.dumps(report,indent=2))
        print('SNAPSHOT_INDEPENDENCE_PASS '+name,flush=True)
    endpoint='/terminal/sessions/'+sid+'/recoveries/'+rid
    database=api.root/'test.sqlite'
    engine=create_async_engine('sqlite+aiosqlite:///'+database.as_posix())
    try:
        async with engine.connect() as db:
            raw_profile=await db.scalar(select(NativeRequest.profile_json).where(NativeRequest.id==rid))
    finally:await engine.dispose()
    profile=json.loads(raw_profile)
    root=api.root.resolve()
    source=Path(profile['image_root'])
    assert root.parent==SERVER.parents[1]/'tmp' and root.name.startswith('sandbox-recovery-ui-retirement-')
    assert source.resolve()==source and source.parent==root/'files'/'.terminal-export'
    assert len(source.name)==32 and uuid.UUID(source.name).hex==source.name
    quarantine=root/'quarantined-source'
    quarantine.mkdir(exist_ok=False)
    preserved=quarantine/source.name
    assert preserved.resolve().is_relative_to(root) and not preserved.exists()
    initial=await call('GET',endpoint)
    archive=await call('GET',endpoint+'/download',raw=True)
    assert hashlib.sha256(archive).hexdigest()==initial['receipt']['archive_sha256']
    retired=await call('GET','/terminal/sessions/'+sid+'/retirement')
    assert retired['status']=='retired'
    moved=False
    try:
        source.rename(preserved);moved=True
        assert not source.exists() and preserved.is_dir()
        report['actual_source_directory_absent']=True
        assert await call('GET',endpoint)==initial
        assert await call('GET',endpoint+'/download',raw=True)==archive
        assert await call('GET','/terminal/sessions/'+sid+'/retirement')==retired
        record('snapshot_manifest_download_and_retirement_survive_actual_source_absence')
        if continuation is not None:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('actual_model_reads_confirmed_restored_content_without_source_image','real_llm')
        else:
            path='Ref/retirement.txt'
            preview=await call('POST',endpoint+'/restore-plan',body={'paths':[path]})
            assert preview['files'][0]['action']=='create'
            result=await call('POST',endpoint+'/restore',body={'paths':[path],'plan_sha256':preview['plan_sha256'],
                'request_id':str(uuid.uuid4()),'confirm':True,'semantics':'file_contents_only'})
            assert result['written_files']==[path]
            assert await call('GET',endpoint+'/restores/'+result['request_id'])=={k:v for k,v in result.items() if k!='replayed'}
        assert await call('GET',api.prefix+'/raw?path=Ref/retirement.txt',raw=True)==(proof+'\n').encode()
        record('confirmed_file_restore_uses_retained_snapshot_without_source_image')
        snapshot_zip=Path(profile['root'])/'recovery.zip'
        original=snapshot_zip.read_bytes()
        try:
            snapshot_zip.write_bytes(original[:-1])
            await call('GET',endpoint+'/download',expect=409)
        finally:snapshot_zip.write_bytes(original)
        record('missing_source_does_not_bypass_snapshot_archive_integrity','protocol_fault')
        async with engine.connect() as db:
            before=list(await db.scalars(select(NativeRequest.id).where(NativeRequest.case_id==api.case_id)))
        await call('POST','/terminal/sessions/'+sid+'/recoveries',expect=502)
        async with engine.connect() as db:
            after=list(await db.scalars(select(NativeRequest.id).where(NativeRequest.case_id==api.case_id)))
        assert sorted(before)==sorted(after),'Missing image must fail before reserving native execution'
        record('new_image_recovery_still_refuses_missing_source_before_native_execution','protocol_fault')
        assert not source.exists()
        report['passed']=True
    finally:
        await engine.dispose()
        if moved:
            assert not source.exists() and source.resolve().is_relative_to(root) and preserved.resolve().is_relative_to(root)
            preserved.rename(source)
            report['source_restored_after_test']=True
        trace=api.root/'upstream.jsonl'
        if trace.exists():
            upstream=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in upstream)
        target.write_text(json.dumps(report,indent=2))
    return report['llm_calls']


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    if args.s3:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
            asyncio.run(verify(store,after_retirement=without_image))
    else:asyncio.run(verify(after_retirement=without_image))
