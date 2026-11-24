"""Real S3 streaming staging admission, peer claims, source races and API cleanup."""
import argparse,asyncio,json,os,subprocess,sys,tempfile,threading,time
from pathlib import Path
from testsupport import SERVER,IsolatedServer
from node_storage_budget_smoke import stop,rejected


def configure(root,settings):
    os.environ.update(settings,VIBEFLOW_STORAGE_FS_ROOT=str(root),VIBEFLOW_NODE_STORAGE_LIMIT_MB='1',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
    from app.config import get_settings
    get_settings.cache_clear()
    root.mkdir(parents=True,exist_ok=True)


def child(configuration):
    value=json.loads(configuration.read_text());root=Path(value['root']);configure(root,value['settings'])
    from app import storage
    from app.services import staging_download as download
    original=download.download
    def copying(*args):
        result=original(*args)
        (root.parent/'ready').touch()
        while True:time.sleep(.02)
        return result
    download.download=copying
    storage.prepare_job_staging(value['prefix'],1)


async def verify(store,report):
    from app import storage
    from app.services import staging_download as download,node_storage_budget as budget,process_identity_native as identity
    from app.services.staging_preparation import RECEIPT_PEAK_BYTES
    from app.services.case_write import CaseWriteConflict
    from app.services.async_ops import finish_before_cancel
    settings=store.settings();checks=[];evidence=[]
    storage._client=store.client
    def setup(mode,data=None):
        root=report/mode/'files';configure(root,settings)
        prefix=f'workspaces/9001/cases/{len(checks)+1}'
        for path,content in (data or {'a.bin':b'a'*(300*1024),'b.bin':b'b'*(300*1024)}).items():
            store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+path,Body=content)
        return root,prefix
    def claims(root):return list((root/budget.DIRECTORY).glob('*.json'))
    def stages(root):return list((root/'.staging').glob('*'))
    def passed(name,detail=None):
        checks.append(name);evidence.append({'mode':name,'detail':detail});print('PASS staging download '+name,flush=True)
        (report/'checks.json').write_text(json.dumps({'passed':False,'checks':checks,'evidence':evidence},indent=2),encoding='utf8')
    root,prefix=setup('normal',{'a.bin':b'a'*(600*1024),'文档/empty.txt':b''})
    store.client.put_object(Bucket=store.bucket,Key=prefix+'/.vibeflow/job-logs/job-999.log',Body=b'x'*(2*1024*1024))
    original=download.download;seen=[]
    def copying(*args):
        rows=[json.loads(p.read_text()) for p in claims(root)]
        assert len(rows)==1
        seen.append(rows[0]['bytes']);return original(*args)
    download.download=copying
    try:stage=storage.prepare_job_staging(prefix,1)
    finally:download.download=original
    manifest=storage._staging_manifest_path(stage)
    assert seen==[600*1024+manifest.stat().st_size+RECEIPT_PEAK_BYTES]*2 and not claims(root)
    assert storage._fs_path(stage,'a.bin').read_bytes()==b'a'*(600*1024)
    assert storage._fs_path(stage,'文档/empty.txt').stat().st_size==0
    assert not storage._fs_path(stage,'.vibeflow/job-logs').exists()
    passed('payload_manifest_exact_reservation_empty_unicode_and_private_exclusion',{'reserved':seen[0]})
    root,prefix=setup('full');(root/'occupied.bin').write_bytes(b'x'*(500*1024));called=[]
    download.download=lambda *args:called.append(args)
    try:rejected(lambda:storage.prepare_job_staging(prefix,2))
    finally:download.download=original
    assert not called and not stages(root) and not claims(root)
    passed('node_full_rejects_before_payload_reader_or_staging_creation')
    root,prefix=setup('metadata',{'payload.bin':b'x'*(1000*1024),**{('长文件名'*10+str(i)):b'' for i in range(80)}})
    download.download=lambda *args:called.append(args)
    try:rejected(lambda:storage.prepare_job_staging(prefix,3))
    finally:download.download=original
    assert not called and not stages(root) and not claims(root)
    passed('metadata_peak_is_admitted_in_addition_to_payload')
    root,prefix=setup('unknown');parent=root/budget.DIRECTORY;parent.mkdir();(parent/'unknown.json').write_text('{}')
    download.download=lambda *args:called.append(args)
    try:rejected(lambda:storage.prepare_job_staging(prefix,4),'node_storage_unavailable')
    finally:download.download=original
    assert not called and not stages(root)
    passed('unverifiable_node_claim_rejects_before_copy')
    for mode in ('changed_before_read','changed_after_read','truncated','read_failure'):
        root,prefix=setup(mode);first=True
        actual_get=storage.s3().get_object;reads=[];closed=[]
        class Body:
            def __init__(self,body):self.body=body;self.first=True
            def read(self,size):
                assert 0<size<=1024*1024;reads.append(size)
                if mode=='read_failure':raise OSError('Injected body failure')
                if mode=='truncated' and not self.first:return b''
                self.first=False
                return self.body.read(min(size,1024) if mode=='truncated' else size)
            def close(self):closed.append(True);self.body.close()
        def get(**args):
            response=actual_get(**args);response['Body']=Body(response['Body']);return response
        def copying(prefix,path,record,target):
            nonlocal first
            if first and mode=='changed_before_read':
                store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+path,Body=b'z'*record['size'])
            result=original(prefix,path,record,target)
            if first and mode=='changed_after_read':
                store.client.put_object(Bucket=store.bucket,Key=prefix+'/new.txt',Body=b'late source')
            first=False;return result
        download.download=copying;storage.s3().get_object=get
        try:
            try:storage.prepare_job_staging(prefix,5)
            except (ValueError,OSError):pass
            else:raise AssertionError('Changed or incomplete source was accepted')
        finally:download.download=original;storage.s3().get_object=actual_get
        assert not stages(root) and not claims(root)
        if mode!='changed_before_read':assert reads and closed
        passed(mode,{'bounded_read_requests':reads,'closed_bodies':len(closed)})
    root,prefix=setup('cancel');ready=threading.Event();release=threading.Event();first=True
    def copying(*args):
        nonlocal first
        result=original(*args)
        if first:
            first=False;ready.set()
            if not release.wait(20):raise TimeoutError('Test did not release reader')
        return result
    download.download=copying
    task=asyncio.create_task(finish_before_cancel(asyncio.to_thread(storage.prepare_job_staging,prefix,6)))
    try:
        deadline=time.monotonic()+10
        while not ready.is_set():
            assert not task.done() and time.monotonic()<deadline;await asyncio.sleep(.01)
        task.cancel();await asyncio.sleep(.05);assert not task.done()
        assert len(claims(root))==1
        rejected(lambda:budget._admit(400*1024))
    finally:release.set()
    try:await task
    except asyncio.CancelledError:pass
    else:raise AssertionError('Cancellation was swallowed')
    finally:download.download=original
    assert not claims(root) and len(list((root/'.staging').glob('*.manifest.json')))==1
    rejected(lambda:budget._admit(500*1024))
    passed('cancel_waits_for_copy_and_manifest_then_releases_claim_but_counts_retained_files')
    root,prefix=setup('death');configuration=root.parent/'child.json'
    configuration.write_text(json.dumps({'root':str(root),'prefix':prefix,'settings':settings}),encoding='utf8')
    with (root.parent/'child.log').open('w',encoding='utf8') as log:
        process=subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,'--child',str(configuration)],stdout=log,stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+20
            while not (root.parent/'ready').exists():
                assert process.poll() is None and time.monotonic()<deadline;await asyncio.sleep(.02)
            row=json.loads(claims(root)[0].read_text());rejected(lambda:budget._admit(400*1024))
            stop(process);deadline=time.monotonic()+5
            while identity.probe(row['owner'])['state']!='exited':
                assert time.monotonic()<deadline;await asyncio.sleep(.02)
            with budget.reserve(600*1024):pass
            assert not claims(root)
            partial=[p for p in (root/'.staging').rglob('a.bin')]
            assert len(partial)==1 and partial[0].stat().st_size==300*1024
            rejected(lambda:budget._admit(800*1024))
            passed('dead_writer_releases_only_future_claim_while_partial_files_remain_charged',{'owner':row['owner'],'observation':identity.probe(row['owner'])})
        finally:
            if process.poll() is None:stop(process)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':evidence}
    (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def job_failure(store,report):
    from publication_finalizing_smoke import INSPECT
    from job_log_storage_smoke import OBSERVE
    from publication_storage_smoke import terminal
    checks=[]
    extra='''
@app.post('/api/v1/fixture/fill-node')
async def fill_node(body:dict):
 from app.config import get_settings
 settings=get_settings();settings.node_storage_limit_mb=1;settings.node_storage_min_free_mb=0
 target=Path(settings.storage_fs_root)
 if body['mode']=='full':(target/'other-cache.bin').write_bytes(b'x'*(600*1024))
 else:
  parent=target/'.node-storage-budget';parent.mkdir(exist_ok=True);(parent/'unknown.json').write_text('{}')
 return {'configured':True}
'''
    for mode in ('full','unknown'):
        api=IsolatedServer('staging-node-job-'+mode,app='stage_fixture:app')
        (api.root/'stage_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\nfrom app.main import app\n'+INSPECT+OBSERVE+extra,encoding='utf8')
        with api:
            case=api.create_case('Staging node '+mode);api.put('input.txt','x'*(600*1024))
            api.call('POST','/fixture/fill-node',{'mode':mode})
            api.call('POST',f"/fixture/log-legacy/{case['id']}",expect=(409,500),raw=True)
            job=api.call('GET',api.prefix+'/jobs')[0];final=terminal(api,job['id'])
            assert final['status']=='failed' and final['attempt']['reason_code']=='resource_limit',final
            assert not list((api.root/'files/.staging').glob('*'))
            (api.root/'admission-result.json').write_text(json.dumps(final,indent=2),encoding='utf8')
            checks.append({'mode':mode,'api':str(api.root)})
            print('PASS Job staging node '+mode,flush=True)
    return {'passed':True,'count':len(checks),'checks':checks}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--child',type=Path);parser.add_argument('--only',choices=['core','job']);args=parser.parse_args()
    if args.child:child(args.child);return
    from s3testsupport import ObjectStore
    from s3_typed_publication_smoke import RUNTIME
    report=Path(tempfile.mkdtemp(prefix='staging-node-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={}
    with ObjectStore(executable=RUNTIME) as store:
        if args.only!='job':result['core']=await verify(store,report)
        if args.only!='core':result['job']=await asyncio.to_thread(job_failure,store,report)
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('STAGING_DOWNLOAD_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
