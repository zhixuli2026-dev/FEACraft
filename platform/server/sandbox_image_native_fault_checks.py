"""Real native deletion/receipt interruptions in a pinned isolated source copy."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
from testsupport import SERVER

FAULT_HELPER = '''
def _image_fault(stage,root):
    if root.parent.name!='.terminal-image-cleanup':return
    armed=root.parents[2]/'native-image-fault.json'
    if not armed.exists() or json.loads(armed.read_text())['stage']!=stage:return
    armed.unlink()
    value={'stage':stage,'root':str(root),'birth':module('process_identity_native').observe(os.getpid()),
           'receipt_exists':(root/'complete.json').exists(),'pending_exists':(root/'complete.json.pending').exists()}
    with (root.parents[2]/('native-image-fault-'+stage+'.json')).open('x') as stream:
        stream.write(json.dumps(value));stream.flush();os.fsync(stream.fileno())
    if stage=='directory_sync_error':raise OSError(5,'Injected directory fsync I/O failure')
    os._exit(82)

'''


def source_copy():
    """Copy Python and fixed shell helpers; never copy keys or runtime data."""
    root=Path(tempfile.mkdtemp(prefix='native-image-source-',dir=SERVER.parents[1]/'tmp'))
    # Fixed sandbox launch validation requires the app/services suffix too.
    destination=root/'app/services';destination.mkdir(parents=True)
    original=SERVER/'app/services'; hashes={}
    for path in original.rglob('*'):
        relative=path.relative_to(original)
        if '__pycache__' in relative.parts or path.suffix not in {'.py','.sh'} or not path.is_file():continue
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        raw=path.read_bytes();target.write_bytes(raw);hashes[relative.as_posix()]=hashlib.sha256(raw).hexdigest()
    native=destination/'sandbox_image_cleanup_native.py';code=native.read_text(encoding='utf8')
    def replace(before,after):
        nonlocal code
        assert code.count(before)==1,before
        code=code.replace(before,after)
    replace("SCHEMA = 'vibeflow.sandbox-image-cleanup.v1'",FAULT_HELPER+"SCHEMA = 'vibeflow.sandbox-image-cleanup.v1'")
    replace("                os.unlink('volume.ext4',dir_fd=parent);deleted=True",
            "                _image_fault('before_unlink',root)\n                os.unlink('volume.ext4',dir_fd=parent);deleted=True\n                _image_fault('after_unlink',root)")
    replace('    try:os.fsync(directory)',"    try:\n        _image_fault('directory_sync_error',root)\n        os.fsync(directory)\n        _image_fault('after_receipt_sync',root)")
    compile(code,str(native),'exec');native.write_text(code,encoding='utf8')
    exporter=destination/'sandbox_export_native.py';code=exporter.read_text(encoding='utf8')
    before='    os.rename(temporary, root / name)'
    assert code.count(before)==1
    hook="    if name=='complete.json' and root.parent.name=='.terminal-image-cleanup':\n        load_module('sandbox_image_cleanup_native')._image_fault('%s',root)\n"
    code=code.replace(before,hook%'before_receipt_rename'+before+'\n'+hook%'after_receipt_rename')
    compile(code,str(exporter),'exec');exporter.write_text(code,encoding='utf8')
    manifest={'original':hashes,'instrumented':{name:hashlib.sha256((destination/name).read_bytes()).hexdigest()
        for name in hashes},'fault_helper':FAULT_HELPER,'production_modified_by_fixture':False}
    changed=[name for name in hashes if hashes[name]!=manifest['instrumented'][name]]
    assert set(changed)=={'sandbox_image_cleanup_native.py','sandbox_export_native.py'}
    # Skill lookup is relative to the service source tree. Keep the shipped
    # prompt assets too, so a relocated service does not silently lose them.
    assets={}
    for path in (SERVER/'skills').rglob('*'):
        if not path.is_file() or path.is_symlink() or path.suffix not in {'.md','.yaml','.yml','.json'}:continue
        relative=path.relative_to(SERVER);target=root/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        raw=path.read_bytes();target.write_bytes(raw);assets[relative.as_posix()]=hashlib.sha256(raw).hexdigest()
    manifest['shipped_skill_assets']=assets
    (root/'source-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    prefix='import app.services\napp.services.__path__.insert(0,'+repr(str(destination))+')\n'
    return prefix,root


async def after_retirement(api,sid,rid,proof,continuation=None,source_root=None):
    report={'passed':False,'checks':[],'faults':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite','source_copy':str(source_root),
        'instrumentation':'actual native exit; directory_sync_error is explicit protocol fault'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('NATIVE_IMAGE_FAULT_PASS '+name,flush=True)
    async def rows():
        async with sessions() as db:
            values=list(await db.scalars(select(NativeRequest).where(NativeRequest.kind=='terminal.cleanup')))
            return {row.id:{'phase':row.phase,'error':row.error,'profile':json.loads(row.profile_json),
                'children_json':row.children_json,'result_json':row.result_json,
                'owner':json.loads(row.owner_identity_json),'child':next(iter(scopes.records.document(row)['children'].values())),
                'scope':await scopes.evidence(db,row,next(iter(scopes.records.document(row)['children'].values())))} for row in values}
    try:
        async with sessions() as db:selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip'
        retained=archive.read_bytes()
        stages=('before_unlink','after_unlink','before_receipt_rename','after_receipt_rename','directory_sync_error','after_receipt_sync')
        for stage in stages:
            before=await rows()
            (api.root/'native-image-fault.json').write_text(json.dumps({'stage':stage}))
            await call('POST',endpoint,body=body,expect=409)
            fault=json.loads((api.root/('native-image-fault-'+stage+'.json')).read_text())
            all_rows=await rows();created=set(all_rows)-set(before);assert len(created)==1
            identifier=created.pop();row=all_rows[identifier];child=row['child']
            assert wsl_path(row['profile']['root'])==fault['root']
            assert row['phase']=='failed' and row['error']=='ChildExecutionError'
            assert child['identity']['native']['process']==fault['birth'] and child['stop']['verified'] is True
            assert row['scope']['closed'] is not None and row['scope']['closed']['receipt']['result']['state']=='absent'
            assert identity.probe(row['owner'])['state']=='match'
            exits={'host':identity.probe(child['identity']['host']),
                'native':await probe_native(child['identity']['native']['distribution'],fault['birth'])}
            assert all(v['state']=='exited' for v in exits.values())
            assert image.exists()==(stage=='before_unlink') and archive.read_bytes()==retained
            target=Path(row['profile']['root']);receipt=target/'complete.json';pending=target/'complete.json.pending'
            assert pending.exists()==(stage=='before_receipt_rename')
            assert receipt.exists()==(stage in {'after_receipt_rename','directory_sync_error','after_receipt_sync'})
            if receipt.exists():
                value=json.loads(receipt.read_bytes())
                assert value['outer_gate']==fault['birth'] and value['deleted_this_request'] is False
            assert (await call('GET',endpoint))['status']=='pending'
            await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,expect=409)
            assert await rows()==all_rows,'Read-only status changed failed native records'
            fault.update(request_id=identifier,persisted=row,actual_exits=exits,image_exists=image.exists())
            report['faults'].append(fault)
            record(stage+'_failed_request_never_claims_completion',
                   'protocol_fault' if stage=='directory_sync_error' else 'actual_infrastructure')
        before=await rows();done=await call('POST',endpoint,body=body)
        assert done['status']=='completed' and done['image_absent'] and done['deleted_this_request'] is False
        final=await rows()
        for identifier,row in before.items():assert final[identifier]==row
        assert not image.exists() and archive.read_bytes()==retained
        assert await call('POST',endpoint,body=body)==done and await call('GET',endpoint)==done
        async with sessions() as db:
            completed=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
            reconciled=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_abandoned')))
            assert len(completed)==1 and not reconciled
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        report['infrastructure_passed']=True;report['completion']=done
        record('retry_of_physically_deleted_image_finishes_once_without_inventing_earlier_success')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_retained_snapshot_after_native_failures','real_llm')
        report['passed']=True
        return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]}
        raise
    finally:
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions')
            for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'native-image-fault-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
