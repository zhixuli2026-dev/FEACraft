"""Real S3 publication copies, process death, fresh caches and corruption refusal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

from s3testsupport import ObjectStore
from testsupport import SERVER


def child(root, mode):
    context = json.loads((root/'context.json').read_text(encoding='utf8'))
    os.environ.update(context['settings'])
    os.environ['VIBEFLOW_STORAGE_FS_ROOT'] = str(root/('cache-'+mode))
    from app import storage
    from app.services import artifact_publication as pub, publication_journal as journal
    from app.services import attempt_workspace as workspace, attempt_objects as objects
    from app.services.publication_inputs import inspect_files
    from app.services.durable_events import json_text
    row = SimpleNamespace(**context['row']); run = root/'run'; prefix = context['prefix']
    records = workspace.inventory(run, ['a.txt','nested']) if context['records'] is None else context['records']
    descriptor = context['descriptor']
    manifest = journal.bundle_manifest(row,records,descriptor,prefix=prefix)
    digest = hashlib.sha256(json_text(manifest).encode('utf8')).hexdigest()
    def barrier():
        (root/(mode+'-ready-'+str(os.getpid()))).touch()
        deadline=time.monotonic()+20
        while not (root/(mode+'-release')).exists():
            if time.monotonic()>deadline: raise TimeoutError('Concurrent test barrier expired')
            time.sleep(.02)
    if mode=='concurrent_prepare':
        put=objects.put_new
        def synchronized_put(prefix,path,data):
            if path.endswith('/files/a.txt'): barrier()
            return put(prefix,path,data)
        objects.put_new=synchronized_put
    if mode=='concurrent_read':
        from app.services import publication_objects as remote
        move=remote.move_new
        def synchronized_move(*args,**kwargs):
            barrier()
            return move(*args,**kwargs)
        remote.move_new=synchronized_move
    if mode in {'partial','before_ready'}:
        put = objects.put_new
        def crash(prefix, path, data):
            put(prefix,path,data)
            if (mode=='partial' and path.endswith('/files/a.txt') or
                    mode=='before_ready' and path.endswith('/manifest.json')):
                os._exit(41)
        objects.put_new = crash
    if mode == 'inspect':
        result = journal.inspect(prefix,row)
    elif mode in {'read','concurrent_read','cache_corrupt','remote_corrupt_cached','wrong_digest','wrong_owner','wrong_binding'}:
        row.manifest_sha256 = digest
        if mode=='wrong_digest': row.manifest_sha256='f'*64
        if mode=='wrong_owner': row.case_id += 1
        if mode=='wrong_binding': prefix += '-foreign'
        folder, observed = pub.read_manifest(prefix,row)
        assert observed == manifest
        assert pub.read_file(prefix,row,'a.txt') == (run/'a.txt').read_bytes()
        selected, files, _ = inspect_files(prefix,row,['nested/b.txt'])
        assert (selected/'nested/b.txt').read_bytes() == (run/'nested/b.txt').read_bytes()
        assert set(files) == {'nested/b.txt'}
        if mode=='cache_corrupt':
            (folder/'files/a.txt').write_bytes(b'local corruption')
            pub.read_manifest(prefix,row)
            raise AssertionError('Corrupted cache accepted')
        if mode=='remote_corrupt_cached':
            storage._s3_put(prefix,row.immutable_path+'/files/a.txt',b'remote corruption')
            pub.read_manifest(prefix,row)
            raise AssertionError('Remote corruption hidden by cache')
        result={'state':'read_verified', 'cache':str(folder), 'manifest_sha256':digest}
    else:
        value = pub.prepare_bundle(prefix,row,run,records,descriptor)
        assert value == digest
        result = {'state':'prepared', 'manifest_sha256':value, 'diagnosis':journal.inspect(prefix,row)}
        assert result['diagnosis']['state']=='bundle_verified'
    (root/('result-'+mode+'.json')).write_text(json.dumps(result,indent=2),encoding='utf8')


def launch(root, mode, expected=0):
    result=subprocess.run([sys.executable,'-X','utf8','-B',__file__,'--child',str(root),'--mode',mode],
        cwd=SERVER,capture_output=True,text=True,encoding='utf8',timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    (root/(mode+'.log')).write_text(result.stdout+result.stderr,encoding='utf8')
    assert result.returncode==expected,(mode,result.returncode,result.stderr)
    if expected==1:
        assert 'ValueError:' in result.stderr or 'CaseWriteConflict:' in result.stderr,result.stderr


def fixture(store,name,records=None):
    root=store.root/name;root.mkdir();(root/'run/nested').mkdir(parents=True)
    (root/'run/a.txt').write_bytes(b'A'*8192)
    (root/'run/nested/b.txt').write_bytes('专业结果\n'.encode('utf8')*100)
    (root/'run/nested/empty.txt').write_bytes(b'')
    identifier=str(uuid.uuid4())
    context={'settings':store.settings(),'prefix':'cases/'+uuid.uuid4().hex,
        'row':{'id':identifier,'case_id':17,'attempt_id':str(uuid.uuid4()),'kind':'report','generation':1,
            'immutable_path':'.vibeflow/publications/'+identifier,'manifest_sha256':'0'*64,'status':'prepared'},
        'descriptor':{'handler':'fixture.v1','source':'fixed'},'records':records}
    write_context(root,context)
    return root,context


def write_context(root,context):
    (root/'context.json').write_text(json.dumps(context),encoding='utf8')


def inventory(store,context):
    prefix=context['prefix']+'/'
    values=store.client.list_objects_v2(Bucket=store.bucket,Prefix=prefix).get('Contents',[])
    return {item['Key']:(item['ETag'],item['LastModified'].isoformat(),item['Size']) for item in values}


def concurrent(root,mode):
    children=[];logs=[]
    try:
        for index in range(2):
            log=(root/(mode+'-'+str(index)+'.log')).open('w',encoding='utf8');logs.append(log)
            children.append(subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,
                '--child',str(root),'--mode',mode],cwd=SERVER,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
        deadline=time.monotonic()+15
        while len(list(root.glob(mode+'-ready-*')))!=2:
            assert all(p.poll() is None for p in children),'Concurrent test exited before barrier'
            assert time.monotonic()<deadline,'Concurrent test did not reach barrier'
            time.sleep(.02)
        (root/(mode+'-release')).touch()
        for proc in children: assert proc.wait(timeout=30)==0,mode
    finally:
        for proc in children:
            if proc.poll() is None: proc.terminate();proc.wait(timeout=10)
        for log in logs: log.close()


def verify():
    checks=[]
    with ObjectStore() as store:
        for mode in ('partial','before_ready'):
            root,context=fixture(store,mode)
            launch(root,mode,41);launch(root,'inspect')
            diagnosis=json.loads((root/'result-inspect.json').read_text())
            assert diagnosis['state']==('copy_incomplete' if mode=='partial' else 'bundle_verified'),diagnosis
            before=inventory(store,context)
            launch(root,'recover');launch(root,'read')
            after=inventory(store,context)
            assert all(after[key]==value for key,value in before.items()),'Existing immutable bytes were overwritten'
            launch(root,'repeat');assert inventory(store,context)==after
            checks.append(mode+'_real_process_death_resume_and_fresh_cache')
        root,context=fixture(store,'read_checks');launch(root,'initial')
        before=inventory(store,context)
        for mode in ('wrong_digest','wrong_owner','cache_corrupt'):
            launch(root,mode,1);assert inventory(store,context)==before
            checks.append(mode+'_rejected_without_remote_mutation')
        # Same remote bytes under another prefix cannot be rebound by the cache.
        for key in before:
            data=store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read()
            store.client.put_object(Bucket=store.bucket,Key=key.replace(context['prefix']+'/',context['prefix']+'-foreign/',1),Body=data)
        launch(root,'wrong_binding',1);checks.append('copied_objects_cannot_change_storage_binding')
        launch(root,'remote_corrupt_cached',1);launch(root,'inspect')
        assert json.loads((root/'result-inspect.json').read_text())['state']=='requires_attention'
        damaged=inventory(store,context);launch(root,'reject',1)
        assert inventory(store,context)==damaged
        checks.append('remote_corruption_rejected_even_with_valid_cache_and_never_overwritten')
        root,context=fixture(store,'intent_conflict');launch(root,'partial',41)
        before=inventory(store,context);context['descriptor']['source']='changed';write_context(root,context)
        launch(root,'reject',1);assert inventory(store,context)==before
        checks.append('different_intent_cannot_resume_partial_objects')
        root,context=fixture(store,'extra_object');launch(root,'initial')
        key=context['prefix']+'/'+context['row']['immutable_path']+'/files/extra.txt'
        store.client.put_object(Bucket=store.bucket,Key=key,Body=b'extra')
        launch(root,'read',1);checks.append('extra_remote_file_rejected')
        root,context=fixture(store,'missing_committed_manifest');launch(root,'initial')
        context['row']['manifest_sha256']=json.loads((root/'result-initial.json').read_text())['manifest_sha256']
        context['row']['status']='committed';write_context(root,context)
        store.client.delete_object(Bucket=store.bucket,Key=context['prefix']+'/'+context['row']['immutable_path']+'/manifest.json')
        launch(root,'inspect');assert json.loads((root/'result-inspect.json').read_text())['state']=='requires_attention'
        checks.append('missing_committed_manifest_is_not_resumable_preparation')
        good={'size':1,'sha256':hashlib.sha256(b'a').hexdigest(),'executable':False}
        for name,records in [('traversal',{'../outside':good}),('case_collision',{'A':good,'a':good}),
                             ('file_directory_collision',{'a':good,'a/b':good}),('size_limit',{'a':{**good,'size':1024**3+1}})]:
            root,context=fixture(store,name,records);launch(root,'reject',1)
            assert not inventory(store,context)
            checks.append(name+'_rejected_before_object_creation')
        root,context=fixture(store,'empty_bundle',{});launch(root,'initial')
        checks.append('empty_output_bundle_is_valid')
        root,context=fixture(store,'concurrent')
        concurrent(root,'concurrent_prepare');before=inventory(store,context)
        concurrent(root,'concurrent_read');assert inventory(store,context)==before
        assert not list((root/'cache-concurrent_read').rglob('.restoring-*'))
        checks.append('two_processes_conditionally_prepare_same_bundle')
        checks.append('two_processes_restore_same_cache_without_overwrite_or_stage_leak')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root),
            'scope':'object copying and reading only; SQL publication activation and mutable aliases not enabled'}
    (store.root/'publication-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_PUBLICATION_OBJECTS_OK '+json.dumps(result),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--child',type=Path);parser.add_argument('--mode');args=parser.parse_args()
    child(args.child,args.mode) if args.child else verify()
