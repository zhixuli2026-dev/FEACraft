"""Actual SeaweedFS immutable-input upload and conflict/drain acceptance.

No model call or model response is involved. All object operations use the
production conditional create/read/hash functions against a private real store.
"""
import hashlib,json,os,tempfile,threading,uuid
from pathlib import Path

from testsupport import SERVER
from s3testsupport import ObjectStore


def main():
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=16)
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'kind':'actual_object_storage_and_failure_checks'}
    print('IMMUTABLE_INPUT_UPLOAD_EVIDENCE '+str(store.root),flush=True)
    try:
        with store:
            os.environ.update(store.settings())
            # ObjectStore owns the remote service; this direct infrastructure
            # entry must also isolate its newly exercised local cache storage.
            os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(store.root/'files')
            os.environ['VIBEFLOW_CODEX_STATE_ROOT']=str(store.root/'cli-state')
            os.environ['VIBEFLOW_CODEX_WSL_STATE_ROOT']=''
            from app.config import get_settings
            get_settings.cache_clear()
            from app.services import attempt_objects as objects
            from app.services.case_write import CaseWriteConflict
            root=Path(tempfile.mkdtemp(prefix='input-upload-source-',dir=store.root))
            prefix='workspaces/1/cases/1';base='.vibeflow/attempts/'+str(uuid.uuid4())+'/inputs'
            files={}
            for i in range(24):
                path='Ref/input-'+str(i)+'.txt';data=('ACTUAL_INPUT_'+str(i)).encode()
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
                files[path]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
            objects.put_input_members(prefix,base,files,root)
            before={path:objects.read(prefix,base+'/'+path) for path in files}
            assert all(objects.digest(data)=={k:files[path][k] for k in ('size','sha256')} for path,data in before.items())
            objects.put_input_members(prefix,base,files,root)
            assert all(objects.read(prefix,base+'/'+path)==data for path,data in before.items())
            assert not any(t.name.startswith('immutable-input-write') for t in threading.enumerate())

            from app.services.private_cache import Cache
            cache=Cache('.case-read-views')
            try:
                cache.fetch_many((path,record['size'],lambda path=path,record=record:
                    objects.read(prefix,base+'/'+path,limit=record['size'])) for path,record in files.items())
                assert all((cache.root/path).read_bytes()==data for path,data in before.items())
                assert not any(t.name.startswith('case-input-read') for t in threading.enumerate())
            finally:cache.cleanup()
            bad_cache=Cache('.case-read-views');first=next(iter(files))
            try:
                try:bad_cache.fetch_many((path,record['size']+(path==first),lambda path=path,record=record:
                    objects.read(prefix,base+'/'+path,limit=record['size'])) for path,record in files.items())
                except ValueError:pass
                else:raise AssertionError('Incorrect cache admission size was accepted')
                assert not any(t.name.startswith('case-input-read') for t in threading.enumerate())
            finally:bad_cache.cleanup()

            from app.services import attempt_workspace as workspace
            from app import storage
            identifier=base.split('/')[2];relative='.vibeflow/attempts/'+identifier
            manifest={'schema':workspace.SCHEMA,'attempt_id':identifier,'inputs_path':base,
                'run_path':relative+'/run','selection':['Ref'],'files':files,'directories':['Ref'],
                'source':{'actual_infrastructure_acceptance':True},'storage':objects.binding(prefix)}
            objects.put_new(prefix,relative+'/manifest.json',json.dumps(manifest).encode())
            hydrated=workspace.verify_inputs(prefix,manifest)
            assert all((hydrated/path).read_bytes()==data for path,data in before.items())
            assert workspace.verify_inputs(prefix,manifest)==hydrated
            # Actually corrupt one private remote input while retaining the
            # valid warm local cache. Every call must still check remote bytes.
            args=objects.arguments(prefix,base+'/'+first);client=storage.s3()
            etag=client.head_object(**args)['ETag']
            client.put_object(**args,Body=b'X'*len(before[first]),IfMatch=etag)
            try:workspace.verify_inputs(prefix,manifest)
            except CaseWriteConflict:pass
            else:raise AssertionError('Warm cache hid actual remote input corruption')
            assert (hydrated/first).read_bytes()==before[first]
            assert not any(t.name.startswith('immutable-object-read') for t in threading.enumerate())

            # A real pre-existing wrong immutable object must survive; the
            # conditional retry verifies its bytes and refuses the new group.
            conflict_base='.vibeflow/attempts/'+str(uuid.uuid4())+'/inputs'
            first=next(iter(files));objects.put_new(prefix,conflict_base+'/'+first,b'EXTERNAL_EXISTING_BYTES')
            try:objects.put_input_members(prefix,conflict_base,files,root)
            except (CaseWriteConflict,ValueError):pass
            else:raise AssertionError('Conflicting immutable object was accepted')
            assert objects.read(prefix,conflict_base+'/'+first)==b'EXTERNAL_EXISTING_BYTES'
            assert not any(t.name.startswith('immutable-input-write') for t in threading.enumerate())

            # Tamper the actual local cache after its record was captured.
            # No upload may substitute different bytes for that record.
            changed_base='.vibeflow/attempts/'+str(uuid.uuid4())+'/inputs'
            (root/first).write_bytes(b'ACTUALLY_CHANGED_SOURCE')
            try:objects.put_input_members(prefix,changed_base,files,root)
            except CaseWriteConflict:pass
            else:raise AssertionError('Changed local source was accepted')
            try:objects.read(prefix,changed_base+'/'+first)
            except FileNotFoundError:pass
            else:raise AssertionError('Changed member reached object storage')
            assert not any(t.name.startswith('immutable-input-write') for t in threading.enumerate())
            report.update(passed=True,actual_members=24,all_members_read_and_hashed=True,
                identical_retry_verified=True,conflicting_remote_bytes_preserved=True,
                changed_local_member_not_uploaded=True,all_started_workers_joined=True,
                actual_read_cache_materialized=True,wrong_cache_size_refused_and_readers_drained=True,
                actual_cold_input_hydration_verified=True,warm_input_verification_repeated=True,
                actual_remote_corruption_refused_with_valid_warm_cache=True)
    finally:
        report['owned_store_stopped']=store.proc is not None and store.proc.poll() is not None
        (store.root/'input-upload-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print('IMMUTABLE_INPUT_UPLOAD_OK '+str(store.root),flush=True)


if __name__=='__main__':main()
