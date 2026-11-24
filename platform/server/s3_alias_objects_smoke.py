"""Real conditional alias races, interrupted repair, and unsupported backend refusal."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from s3testsupport import ObjectStore
from testsupport import SERVER

RUNTIME=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'


def configure(store):
    os.environ.update(store.settings());os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(store.root/'cache')
    from app.config import get_settings
    from app import storage
    get_settings.cache_clear();storage._client=None


def fixture(store,mode,*,extra=0,solve=False):
    from app import storage
    from app.services import artifact_publication as pub, publication_alias_journal as aliases, attempt_workspace as workspace
    prefix='cases/'+uuid.uuid4().hex;run=store.root/mode;run.mkdir()
    (run/'a.txt').write_bytes(b'new a');(run/'b.txt').write_bytes(b'new b')
    (run/'created.txt').write_bytes(b'created')
    for index in range(extra):(run/f'extra-{index}.txt').write_bytes(str(index).encode())
    initial={'a.txt':b'old a','b.txt':b'old b','obsolete.txt':b'obsolete'}
    if solve:initial['retained.txt']=b'retained'
    def rec(data):return {'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
    before={p:rec(data) for p,data in initial.items()}
    for path,data in initial.items():storage._s3_put(prefix,path,data)
    records=workspace.inventory(run,['a.txt','b.txt','created.txt',*(f'extra-{i}.txt' for i in range(extra))])
    descriptor={'handler':'case-command.v1','source':{'files':before,'directories':[]},
        'deleted_files':{'obsolete.txt':before['obsolete.txt']},'output_files':records}
    if solve:
        descriptor={'handler':'solve.v1','retired_aliases':{p:before[p] for p in ('obsolete.txt','retained.txt')}}
        if mode=='solve_before_prepare':storage._s3_put(prefix,'retained.txt',b'user edit')
    identifier=str(uuid.uuid4())
    row=SimpleNamespace(id=identifier,case_id=17,attempt_id=str(uuid.uuid4()),kind='fixture',generation=1,
        immutable_path='.vibeflow/publications/'+identifier,manifest_sha256='0'*64,status='prepared')
    row.manifest_sha256=pub.prepare_bundle(prefix,row,run,records,descriptor)
    _,manifest=pub.read_manifest(prefix,row)
    digest=aliases.prepare(prefix,row,manifest)
    return prefix,row,digest,initial


def verify(store,*,unsupported=False):
    configure(store)
    from app import storage
    from app.services import publication_alias_journal as aliases, s3_case_files as live
    checks=[]
    modes=('unsupported',) if unsupported else ('normal','known_conflict','update_race','create_race','delete_race','interrupted','wrong_plan')
    for mode in modes:
        prefix,row,digest,initial=fixture(store,mode)
        client=storage.s3();put=client.put_object;delete=client.delete_object
        triggered=[]
        if mode=='known_conflict':storage._s3_put(prefix,'b.txt',b'user edit')
        def putting(**kwargs):
            path=kwargs['Key'].removeprefix(prefix+'/')
            selected={'update_race':'a.txt','create_race':'created.txt'}.get(mode)
            if path==selected and not triggered:
                triggered.append(path);put(Bucket=store.bucket,Key=kwargs['Key'],Body=b'concurrent user edit')
            if mode=='interrupted' and path=='b.txt':
                triggered.append(path);raise TimeoutError('Injected partial alias upload')
            return put(**kwargs)
        def deleting(**kwargs):
            if mode=='delete_race' and kwargs['Key']==prefix+'/obsolete.txt' and not triggered:
                triggered.append('obsolete.txt');put(Bucket=store.bucket,Key=kwargs['Key'],Body=b'concurrent user edit')
            return delete(**kwargs)
        client.put_object=putting;client.delete_object=deleting
        error=None
        try:aliases.apply(prefix,row,'f'*64 if mode=='wrong_plan' else digest)
        except (ValueError,TimeoutError) as exc:error=exc
        finally:client.put_object=put;client.delete_object=delete
        if mode=='normal':
            assert error is None,error
            assert live.read_version(prefix,'a.txt')[0]==b'new a'
            assert live.read_version(prefix,'b.txt')[0]==b'new b'
            assert live.read_version(prefix,'created.txt')[0]==b'created'
            assert live.read_version(prefix,'obsolete.txt')[0] is None
            aliases.apply(prefix,row,digest)
        else:
            assert error is not None,mode
            if mode in {'update_race','create_race','delete_race'}:
                assert triggered and live.read_version(prefix,triggered[0])[0]==b'concurrent user edit'
            if mode in {'known_conflict','wrong_plan','unsupported'}:
                for path,data in initial.items():
                    assert live.read_version(prefix,path)[0]==(b'user edit' if mode=='known_conflict' and path=='b.txt' else data)
                assert live.read_version(prefix,'created.txt')[0] is None
            if mode=='interrupted':
                assert live.read_version(prefix,'a.txt')[0]==b'new a'
                assert live.read_version(prefix,'b.txt')[0]==b'old b'
                aliases.apply(prefix,row,digest)
                assert live.read_version(prefix,'b.txt')[0]==b'new b'
                assert live.read_version(prefix,'obsolete.txt')[0] is None
        checks.append(mode+'_conditional_alias_plan')
    if not unsupported:
        from app.services import attempt_objects as objects, publication_objects as bundles
        for mode in ('solve_before_prepare','solve_after_prepare','solve_delete_race','solve_current_conflict'):
            prefix,row,digest,initial=fixture(store,mode,solve=True)
            client=storage.s3();delete=client.delete_object;triggered=[]
            if mode!='solve_delete_race':storage._s3_put(prefix,'retained.txt',b'user edit')
            if mode=='solve_current_conflict':storage._s3_put(prefix,'b.txt',b'current edit')
            def deleting_retired(**kwargs):
                if mode=='solve_delete_race' and kwargs['Key']==prefix+'/retained.txt' and not triggered:
                    triggered.append(True);storage._s3_put(prefix,'retained.txt',b'user edit')
                return delete(**kwargs)
            client.delete_object=deleting_retired
            try:
                for _ in range(2):
                    try:aliases.apply(prefix,row,digest)
                    except ValueError:pass
                    else:raise AssertionError('Solve edit should retain a repair error')
                    assert live.read_version(prefix,'retained.txt')[0]==b'user edit'
                    assert live.read_version(prefix,bundles.journal_path(row,'aliases-applied.json'))[0] is None
            finally:client.delete_object=delete
            if mode=='solve_current_conflict':
                assert live.read_version(prefix,'a.txt')[0]==b'old a'
                assert live.read_version(prefix,'obsolete.txt')[0]==b'obsolete'
                assert live.read_version(prefix,'b.txt')[0]==b'current edit'
            else:
                assert live.read_version(prefix,'obsolete.txt')[0] is None
                assert live.read_version(prefix,'a.txt')[0]==b'new a'
                assert live.read_version(prefix,'b.txt')[0]==b'new b'
                delete(**objects.arguments(prefix,'retained.txt'))
                aliases.apply(prefix,row,digest)
                assert live.read_version(prefix,bundles.journal_path(row,'aliases-applied.json'))[0] is not None
            if mode=='solve_delete_race':assert triggered
            checks.append(mode+'_preserves_edit_and_independently_retires_unchanged_fields')
        prefix,row,digest,initial=fixture(store,'many-files',extra=64)
        _,manifest=bundles.read_manifest(prefix,row)
        original=objects.read;reads=[]
        def counted(*args,**kwargs):reads.append(args[1]);return original(*args,**kwargs)
        objects.read=counted
        try:
            assert aliases.prepare(prefix,row,manifest)==digest
            aliases.apply(prefix,row,digest)
        finally:objects.read=original
        #67 artifact files must not trigger67 full-bundle verifications per phase.
        assert len(reads)<=16*len(manifest['files'])+100,len(reads)
        assert live.read_version(prefix,'extra-63.txt')[0]==b'63'
        checks.append('large_alias_plan_verifies_complete_bundle_with_linear_remote_reads')
        for mode in ('corrupt_before_apply','corrupt_during_apply'):
            prefix,row,digest,initial=fixture(store,mode)
            changed=[]
            if mode=='corrupt_before_apply':storage._s3_put(prefix,row.immutable_path+'/files/b.txt',b'corrupt source')
            def changing(*args,**kwargs):
                path=args[1]
                if mode=='corrupt_during_apply' and path==row.immutable_path+'/files/b.txt':
                    changed.append(path)
                    # First read is complete-bundle verification, second is the
                    # selected read immediately before the conditional write.
                    if len(changed)==2:storage._s3_put(prefix,path,b'corrupt source')
                return original(*args,**kwargs)
            objects.read=changing
            try:
                try:aliases.apply(prefix,row,digest)
                except ValueError:pass
                else:raise AssertionError('Corrupt published source was accepted')
            finally:objects.read=original
            assert live.read_version(prefix,'b.txt')[0]==initial['b.txt']
            if mode=='corrupt_before_apply':
                assert all(live.read_version(prefix,p)[0]==v for p,v in initial.items())
            checks.append(mode+'_never_copies_corrupt_remote_bytes')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root)}


def main():
    with ObjectStore(executable=RUNTIME) as store:result=verify(store)
    (store.root/'alias-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    with ObjectStore() as old:legacy=verify(old,unsupported=True)
    (old.root/'alias-result.json').write_text(json.dumps(legacy,indent=2),encoding='utf8')
    print('S3_ALIAS_OBJECTS_OK '+json.dumps({'current':result,'legacy_refusal':legacy}),flush=True)


if __name__=='__main__':main()
